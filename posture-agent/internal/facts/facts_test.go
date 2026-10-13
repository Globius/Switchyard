package facts

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"
)

func TestVersion(t *testing.T) {
	cases := map[string]string{
		"6.8.0-45-generic": "6.8.0",
		"Microsoft Windows [Version 10.0.22631.4037]": "10.0.22631.4037",
		"14.5\n":    "14.5",
		"no digits": "",
		"1.2.3.4.5": "1.2.3.4",
	}
	for in, want := range cases {
		if got := Version(in); got != want {
			t.Errorf("Version(%q) = %q, want %q", in, got, want)
		}
	}
}

const lsblkLuks = `{"blockdevices":[{"name":"nvme0n1","type":"disk","mountpoints":[null],
 "children":[{"name":"nvme0n1p1","type":"part","mountpoints":["/boot/efi"]},
  {"name":"nvme0n1p3","type":"part","mountpoints":[null],
   "children":[{"name":"luks-1","type":"crypt","mountpoints":["/"]}]}]}]}`

const lsblkPlain = `{"blockdevices":[{"name":"sda","type":"disk","mountpoints":[null],
 "children":[{"name":"sda1","type":"part","mountpoints":["/"]},
  {"name":"sda2","type":"part","mountpoints":["/data"]}]}]}`

const lsblkOnlyData = `{"blockdevices":[{"name":"sda","type":"disk","children":[
 {"name":"sda1","type":"part","mountpoint":"/"},
 {"name":"sdb","type":"disk","children":[{"name":"c","type":"crypt","mountpoint":"/data"}]}]}]}`

func TestLsblkEncrypted(t *testing.T) {
	if !LsblkEncrypted([]byte(lsblkLuks)) {
		t.Error("root on a LUKS device should count")
	}
	if LsblkEncrypted([]byte(lsblkPlain)) {
		t.Error("a plain root must not")
	}
	if LsblkEncrypted([]byte(lsblkOnlyData)) {
		t.Error("an encrypted data disk does not protect the system volume")
	}
	if LsblkEncrypted([]byte("not json")) || LsblkEncrypted(nil) {
		t.Error("garbage is not encrypted")
	}
}

func TestLinuxFirewallParsers(t *testing.T) {
	if !NftActive("table inet filter {\n\tchain input {\n\t\ttype filter hook input priority 0; policy drop;\n\t}\n}") {
		t.Error("nft with a chain")
	}
	if NftActive("") || NftActive("\n") {
		t.Error("empty ruleset")
	}
	if !UfwActive("Status: active\n") || UfwActive("Status: inactive") {
		t.Error("ufw")
	}
	if !FirewalldRunning("running\n") || FirewalldRunning("not running") {
		t.Error("firewalld")
	}
}

const bdeOn = `BitLocker Drive Encryption: Configuration Tool version 10.0.22621
Volume C: [Windows]
    Conversion Status:    Used Space Only Encrypted
    Protection Status:    Protection On
`

const bdeOff = `Volume C: [Windows]
    Conversion Status:    Fully Decrypted
    Protection Status:    Protection Off
`

const netshOn = `
Domain Profile Settings:
----------------------------------------------------------------------
State                                 ON

Private Profile Settings:
----------------------------------------------------------------------
State                                 ON

Public Profile Settings:
----------------------------------------------------------------------
State                                 ON
`

func TestWindowsParsers(t *testing.T) {
	if !ManageBdeProtected(bdeOn) || ManageBdeProtected(bdeOff) || ManageBdeProtected("") {
		t.Error("manage-bde")
	}
	if !NetshAllOn(netshOn) {
		t.Error("all profiles on")
	}
	if NetshAllOn(netshOn+"State                                 OFF\n") || NetshAllOn("") {
		t.Error("one profile off, or no answer")
	}
	reg := "\nHKEY_CURRENT_USER\\Control Panel\\Desktop\n    ScreenSaverIsSecure    REG_SZ    1\n\n"
	if !RegScreenSaverSecure(reg) || RegScreenSaverSecure(reg[:len(reg)-4]+"0\n") {
		t.Error("screen saver")
	}
}

func TestMacParsers(t *testing.T) {
	if !FileVaultOn("FileVault is On.\n") || FileVaultOn("FileVault is Off.") {
		t.Error("filevault")
	}
	if !MacFirewallOn("Firewall is enabled. (State = 1)") || MacFirewallOn("Firewall is disabled. (State = 0)") {
		t.Error("application firewall")
	}
	if on, known := MacScreenLock("2024-01-01 sysadminctl[1:2] screenLock delay is immediate"); !on || !known {
		t.Error("lock on")
	}
	if on, known := MacScreenLock("screenLock is off"); on || !known {
		t.Error("lock off")
	}
	if _, known := MacScreenLock("something else"); known {
		t.Error("unknown answer")
	}
	hist := "Display Name   Version  Date\n----\nmacOS Sonoma 14.4.1    14.4.1   03/21/2024, 10:15:22\nXProtect     5272     04/09/2024, 08:00:01\n"
	got, ok := MacLastUpdate(hist)
	if !ok || got.Format("2006-01-02") != "2024-04-09" {
		t.Errorf("history: %v %v", got, ok)
	}
	if _, ok := MacLastUpdate("nothing"); ok {
		t.Error("no dates")
	}
}

func TestDatesAndDays(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	if d := DaysSince(time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC), now); d != 10 {
		t.Errorf("days %d", d)
	}
	if d := DaysSince(now.Add(48*time.Hour), now); d != 0 {
		t.Errorf("future dates count as zero, got %d", d)
	}
	if got, ok := IsoDate("\r\n2026-09-30\r\n"); !ok || got.Day() != 30 {
		t.Error("iso date")
	}
	if _, ok := IsoDate("x"); ok {
		t.Error("no date")
	}
}

type fakeInfo struct {
	os.FileInfo
	mod time.Time
}

func (f fakeInfo) ModTime() time.Time { return f.mod }

func env(goos string, outputs map[string]string) Env {
	now := time.Date(2026, 10, 12, 0, 0, 0, 0, time.UTC)
	return Env{
		GOOS: goos,
		Run: func(_ context.Context, name string, args ...string) (string, error) {
			if out, ok := outputs[name]; ok {
				return out, nil
			}
			return "", errors.New("not installed")
		},
		ReadFile: func(p string) ([]byte, error) {
			if p == "/proc/sys/kernel/osrelease" {
				return []byte("6.8.0-45-generic\n"), nil
			}
			return nil, os.ErrNotExist
		},
		Stat: func(p string) (os.FileInfo, error) {
			if p == "/var/log/apt/history.log" {
				return fakeInfo{mod: now.Add(-72 * time.Hour)}, nil
			}
			return nil, os.ErrNotExist
		},
		Now:      func() time.Time { return now },
		Hostname: func() (string, error) { return "laptop-ana", nil },
	}
}

func TestCollectLinux(t *testing.T) {
	f, err := Collect(context.Background(), env("linux", map[string]string{"lsblk": lsblkLuks, "ufw": "Status: active"}))
	if err != nil {
		t.Fatal(err)
	}
	if f.OS != "linux" || f.OSVersion != "6.8.0" || !f.DiskEncrypted || !f.FirewallEnabled {
		t.Errorf("%+v", f)
	}
	if f.DaysSinceUpdate == nil || *f.DaysSinceUpdate != 3 || f.ScreenLock != nil {
		t.Errorf("update age / screen lock: %+v", f)
	}
	if f.Hostname == nil || *f.Hostname != "laptop-ana" {
		t.Error("hostname")
	}
	bare, _ := Collect(context.Background(), env("linux", nil))
	if bare.DiskEncrypted || bare.FirewallEnabled {
		t.Error("nothing measured must mean nothing shown")
	}
}

func TestCollectWindows(t *testing.T) {
	f, err := Collect(context.Background(), env("windows", map[string]string{
		"cmd":        "\nMicrosoft Windows [Version 10.0.22631.4037]\n",
		"manage-bde": bdeOn,
		"netsh":      netshOn,
		"reg":        "ScreenSaverIsSecure    REG_SZ    1",
		"powershell": "2026-09-30\r\n",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if f.OSVersion != "10.0.22631.4037" || !f.DiskEncrypted || !f.FirewallEnabled {
		t.Errorf("%+v", f)
	}
	if f.ScreenLock == nil || !*f.ScreenLock || f.DaysSinceUpdate == nil || *f.DaysSinceUpdate != 12 {
		t.Errorf("%+v", f)
	}
	noReg, _ := Collect(context.Background(), env("windows", map[string]string{"cmd": "Version 10.0.1"}))
	if noReg.ScreenLock == nil || *noReg.ScreenLock {
		t.Error("a missing registry value means no secure screen saver")
	}
}

func TestCollectMac(t *testing.T) {
	f, err := Collect(context.Background(), env("darwin", map[string]string{
		"sw_vers":  "14.5\n",
		"fdesetup": "FileVault is On.",
		"/usr/libexec/ApplicationFirewall/socketfilterfw": "Firewall is enabled. (State = 1)",
		"sysadminctl":    "screenLock delay is immediate",
		"softwareupdate": "x 04/09/2024, 08:00:01",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if f.OS != "macos" || f.OSVersion != "14.5" || !f.DiskEncrypted || !f.FirewallEnabled {
		t.Errorf("%+v", f)
	}
	if f.ScreenLock == nil || !*f.ScreenLock || f.DaysSinceUpdate == nil {
		t.Errorf("%+v", f)
	}
}

func TestCollectRefusesWhatItCannotMeasure(t *testing.T) {
	if _, err := Collect(context.Background(), env("plan9", nil)); err == nil {
		t.Error("unsupported system")
	}
	if _, err := Collect(context.Background(), Env{GOOS: "linux", Run: env("linux", nil).Run,
		ReadFile: func(string) ([]byte, error) { return nil, os.ErrNotExist },
		Stat:     func(string) (os.FileInfo, error) { return nil, os.ErrNotExist },
		Now:      time.Now}); err == nil {
		t.Error("no operating system version, no report")
	}
}
