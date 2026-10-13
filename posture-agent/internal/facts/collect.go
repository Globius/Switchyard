package facts

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strings"
	"time"

	"switchyard/posture-agent/internal/protocol"
)

// Runner runs a fixed command and returns its combined output.
type Runner func(ctx context.Context, name string, args ...string) (string, error)

// Env is everything Collect touches outside the process, so tests can replace it.
type Env struct {
	GOOS     string
	Run      Runner
	ReadFile func(string) ([]byte, error)
	Stat     func(string) (os.FileInfo, error)
	Now      func() time.Time
	Hostname func() (string, error)
}

// System returns the real environment.
func System() Env {
	return Env{GOOS: runtime.GOOS, Run: run, ReadFile: os.ReadFile, Stat: os.Stat, Now: time.Now, Hostname: os.Hostname}
}

func run(ctx context.Context, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).CombinedOutput() // #nosec G204 - fixed commands
	return string(out), err
}

var hostRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,253}$`)

// Collect measures the device. A fact that cannot be established stays nil; a requirement that
// needs it then fails at the controller, which is the safe direction.
func Collect(ctx context.Context, env Env) (protocol.Facts, error) {
	f := protocol.Facts{OS: env.GOOS, AgentVersion: protocol.AgentVersion}
	if env.Hostname != nil {
		if h, err := env.Hostname(); err == nil && hostRe.MatchString(h) {
			f.Hostname = &h
		}
	}
	switch env.GOOS {
	case "linux":
		linux(ctx, env, &f)
	case "windows":
		windows(ctx, env, &f)
	case "darwin":
		f.OS = "macos"
		mac(ctx, env, &f)
	default:
		return f, fmt.Errorf("%s is not supported", env.GOOS)
	}
	if f.OSVersion == "" {
		return f, errors.New("could not read the operating system version")
	}
	return f, nil
}

func boolp(b bool) *bool { return &b }

func days(env Env, t time.Time) *int {
	d := DaysSince(t, env.Now())
	return &d
}

func linux(ctx context.Context, env Env, f *protocol.Facts) {
	if raw, err := env.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
		f.OSVersion = Version(string(raw))
	}
	if out, err := env.Run(ctx, "lsblk", "-J", "-o", "NAME,TYPE,MOUNTPOINTS"); err == nil {
		f.DiskEncrypted = LsblkEncrypted([]byte(out))
	}
	if out, err := env.Run(ctx, "nft", "list", "ruleset"); err == nil && NftActive(out) {
		f.FirewallEnabled = true
	} else if out, err := env.Run(ctx, "ufw", "status"); err == nil && UfwActive(out) {
		f.FirewallEnabled = true
	} else if out, err := env.Run(ctx, "firewall-cmd", "--state"); err == nil && FirewalldRunning(out) {
		f.FirewallEnabled = true
	}
	for _, p := range []string{"/var/log/apt/history.log", "/var/lib/dnf/history.sqlite", "/var/lib/pacman/local"} {
		if info, err := env.Stat(p); err == nil {
			f.DaysSinceUpdate = days(env, info.ModTime())
			break
		}
	}
}

func windows(ctx context.Context, env Env, f *protocol.Facts) {
	if out, err := env.Run(ctx, "cmd", "/c", "ver"); err == nil {
		f.OSVersion = Version(out)
	}
	if out, err := env.Run(ctx, "manage-bde", "-status", "C:"); err == nil {
		f.DiskEncrypted = ManageBdeProtected(out)
	}
	if out, err := env.Run(ctx, "netsh", "advfirewall", "show", "allprofiles", "state"); err == nil {
		f.FirewallEnabled = NetshAllOn(out)
	}
	if out, err := env.Run(ctx, "reg", "query", `HKCU\Control Panel\Desktop`, "/v", "ScreenSaverIsSecure"); err == nil {
		f.ScreenLock = boolp(RegScreenSaverSecure(out))
	} else {
		f.ScreenLock = boolp(false) // the value does not exist: no secure screen saver
	}
	script := "(Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1).InstalledOn.ToString('yyyy-MM-dd')"
	if out, err := env.Run(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", script); err == nil {
		if t, ok := IsoDate(out); ok {
			f.DaysSinceUpdate = days(env, t)
		}
	}
}

func mac(ctx context.Context, env Env, f *protocol.Facts) {
	if out, err := env.Run(ctx, "sw_vers", "-productVersion"); err == nil {
		f.OSVersion = Version(strings.TrimSpace(out))
	}
	if out, err := env.Run(ctx, "fdesetup", "status"); err == nil {
		f.DiskEncrypted = FileVaultOn(out)
	}
	if out, err := env.Run(ctx, "/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"); err == nil {
		f.FirewallEnabled = MacFirewallOn(out)
	}
	if out, _ := env.Run(ctx, "sysadminctl", "-screenLock", "status"); out != "" {
		if on, known := MacScreenLock(out); known {
			f.ScreenLock = boolp(on)
		}
	}
	if out, err := env.Run(ctx, "softwareupdate", "--history"); err == nil {
		if t, ok := MacLastUpdate(out); ok {
			f.DaysSinceUpdate = days(env, t)
		}
	}
}
