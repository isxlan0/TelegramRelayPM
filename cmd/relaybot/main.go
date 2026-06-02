package main

import (
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"telegramrelaypm/internal/app"
	"telegramrelaypm/internal/config"
	"telegramrelaypm/internal/store"
	"telegramrelaypm/internal/telegramx"
)

func main() {
	logPath, err := configureLogging()
	if err != nil {
		log.Fatalf("初始化日志失败: %v", err)
	}
	log.Printf("日志文件: %s", logPath)
	if err := terminateSameNameProcesses(); err != nil {
		log.Printf("清理同名旧进程失败，将继续启动: %v", err)
	}

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("读取配置失败: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	client, err := telegramx.New(cfg.BotToken)
	if err != nil {
		log.Fatalf("初始化 Telegram 客户端失败: %v", err)
	}

	db, err := store.OpenSQLite(ctx, cfg.DBPath, cfg.PrimaryAdminChatID)
	if err != nil {
		log.Fatalf("初始化数据库失败: %v", err)
	}
	defer func() {
		if err := db.Close(); err != nil {
			log.Printf("关闭数据库失败: %v", err)
		}
	}()

	if err := client.SetupProfile(ctx, cfg); err != nil {
		log.Printf("同步机器人资料/命令失败，将继续启动: %v", err)
	}
	logStartupDiagnostics(ctx, client, cfg)

	relayApp := app.New(cfg, db, client)
	updates, err := client.Updates(ctx)
	if err != nil {
		log.Fatalf("启动 Telegram 长轮询失败: %v", err)
	}

	log.Printf("机器人启动完成: mode=%s db=%s", cfg.RelayMode, cfg.DBPath)
	for update := range updates {
		relayApp.HandleUpdate(ctx, update)
	}
	log.Println("机器人已停止。")
}

func configureLogging() (string, error) {
	path := time.Now().Format("20060102_150405") + ".log"
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return "", err
	}
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.SetOutput(io.MultiWriter(os.Stdout, file))
	return path, nil
}

func logStartupDiagnostics(ctx context.Context, client *telegramx.TelegoClient, cfg *config.Config) {
	me, err := client.GetMe(ctx)
	if err != nil {
		log.Printf("获取机器人自身信息失败: %v", err)
		return
	}
	log.Printf("机器人自身信息: id=%d username=@%s first_name=%s", me.ID, me.Username, me.FirstName)

	if cfg.RelayMode != config.RelayModeGroupTopic || cfg.AdminGroupChatID == nil {
		return
	}

	chat, err := client.GetChat(ctx, *cfg.AdminGroupChatID)
	if err != nil {
		log.Printf("获取管理员群信息失败: chat=%d err=%v", *cfg.AdminGroupChatID, err)
	} else {
		log.Printf("管理员群信息: id=%d type=%s title=%s is_forum=%t", chat.ID, chat.Type, chat.Title, chat.IsForum)
	}

	member, err := client.GetChatMember(ctx, *cfg.AdminGroupChatID, me.ID)
	if err != nil {
		log.Printf("获取机器人群内权限失败: chat=%d bot=%d err=%v", *cfg.AdminGroupChatID, me.ID, err)
		return
	}
	log.Printf("机器人群内权限: status=%s is_member=%t can_read_all_group_messages=%s", member.MemberStatus(), member.MemberIsMember(), canReadAllGroupMessages(member.MemberStatus()))
}

func canReadAllGroupMessages(status string) string {
	if status == "creator" || status == "administrator" {
		return "true"
	}
	return fmt.Sprintf("unknown(status=%s; 仍需在 BotFather 关闭隐私模式才能读取普通群消息)", status)
}

func terminateSameNameProcesses() error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	processName := filepath.Base(executable)
	currentPID := os.Getpid()

	if runtime.GOOS == "windows" {
		return terminateSameNameProcessesWindows(processName, currentPID)
	}
	return terminateSameNameProcessesUnix(processName, currentPID)
}

func terminateSameNameProcessesWindows(processName string, currentPID int) error {
	output, err := exec.Command("tasklist", "/FI", "IMAGENAME eq "+processName, "/FO", "CSV", "/NH").Output()
	if err != nil {
		return err
	}

	reader := csv.NewReader(strings.NewReader(string(output)))
	reader.FieldsPerRecord = -1
	records, err := reader.ReadAll()
	if err != nil {
		return err
	}

	var errs []error
	for _, record := range records {
		if len(record) < 2 || !strings.EqualFold(strings.TrimSpace(record[0]), processName) {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(record[1]))
		if err != nil || pid == currentPID {
			continue
		}
		if err := exec.Command("taskkill", "/PID", strconv.Itoa(pid), "/T", "/F").Run(); err != nil {
			errs = append(errs, fmt.Errorf("kill pid %d: %w", pid, err))
			continue
		}
		log.Printf("已结束同名旧进程: name=%s pid=%d", processName, pid)
	}
	return errors.Join(errs...)
}

func terminateSameNameProcessesUnix(processName string, currentPID int) error {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}

	var pids []int
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid == currentPID {
			continue
		}
		if unixProcessNameMatches(pid, processName) {
			pids = append(pids, pid)
		}
	}
	if len(pids) == 0 {
		return nil
	}

	var errs []error
	for _, pid := range pids {
		process, err := os.FindProcess(pid)
		if err != nil {
			errs = append(errs, fmt.Errorf("find pid %d: %w", pid, err))
			continue
		}
		if err := process.Signal(syscall.SIGTERM); err != nil {
			errs = append(errs, fmt.Errorf("terminate pid %d: %w", pid, err))
			continue
		}
		log.Printf("已请求结束同名旧进程: name=%s pid=%d", processName, pid)
	}

	time.Sleep(1500 * time.Millisecond)
	for _, pid := range pids {
		if _, err := os.Stat(filepath.Join("/proc", strconv.Itoa(pid))); errors.Is(err, os.ErrNotExist) {
			continue
		}
		process, err := os.FindProcess(pid)
		if err != nil {
			errs = append(errs, fmt.Errorf("find pid %d for force kill: %w", pid, err))
			continue
		}
		if err := process.Kill(); err != nil {
			errs = append(errs, fmt.Errorf("force kill pid %d: %w", pid, err))
			continue
		}
		log.Printf("已强制结束同名旧进程: name=%s pid=%d", processName, pid)
	}
	return errors.Join(errs...)
}

func unixProcessNameMatches(pid int, processName string) bool {
	procDir := filepath.Join("/proc", strconv.Itoa(pid))
	if exePath, err := os.Readlink(filepath.Join(procDir, "exe")); err == nil && filepath.Base(exePath) == processName {
		return true
	}
	comm, err := os.ReadFile(filepath.Join(procDir, "comm"))
	return err == nil && strings.TrimSpace(string(comm)) == processName
}
