// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package facts measures the device: operating system version, disk encryption, firewall, screen
// lock and the age of the last update.
//
// The measuring is done by fixed commands of the operating system (no shell, no user input), and
// every answer is read by one of the pure functions in this file, which the tests exercise with
// real sample output on every platform. A fact the agent cannot establish is null in the report,
// and the controller treats null as "not shown".
package facts

import (
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var versionRe = regexp.MustCompile(`[0-9]{1,6}(\.[0-9]{1,6}){0,3}`)

// Version extracts the first dotted numeric version (at most four parts) from s, or "".
//
//	Version("6.8.0-45-generic") == "6.8.0"
//	Version("Microsoft Windows [Version 10.0.22631.4037]") == "10.0.22631.4037"
func Version(s string) string {
	return versionRe.FindString(s)
}

// LsblkEncrypted reports whether the root file system sits on an encrypted (crypt) device, from
// `lsblk -J -o NAME,TYPE,MOUNTPOINTS`.
func LsblkEncrypted(raw []byte) bool {
	var doc struct {
		Blockdevices []lsblkNode `json:"blockdevices"`
	}
	if json.Unmarshal(raw, &doc) != nil {
		return false
	}
	for _, d := range doc.Blockdevices {
		if d.rootOnCrypt(false) {
			return true
		}
	}
	return false
}

type lsblkNode struct {
	Type        string      `json:"type"`
	Mountpoint  string      `json:"mountpoint"`
	Mountpoints []string    `json:"mountpoints"`
	Children    []lsblkNode `json:"children"`
}

func (n lsblkNode) mountsRoot() bool {
	if n.Mountpoint == "/" {
		return true
	}
	for _, m := range n.Mountpoints {
		if m == "/" {
			return true
		}
	}
	return false
}

// rootOnCrypt is true when "/" is mounted on this node and a crypt device is this node or above it.
func (n lsblkNode) rootOnCrypt(underCrypt bool) bool {
	underCrypt = underCrypt || n.Type == "crypt"
	if n.mountsRoot() && underCrypt {
		return true
	}
	for _, c := range n.Children {
		if c.rootOnCrypt(underCrypt) {
			return true
		}
	}
	return false
}

// NftActive reports whether `nft list ruleset` shows at least one rule or chain with a policy.
func NftActive(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		l := strings.TrimSpace(line)
		if strings.HasPrefix(l, "chain ") || strings.Contains(l, "policy drop") || strings.HasPrefix(l, "ct state") {
			return true
		}
	}
	return false
}

// UfwActive reports whether `ufw status` says the firewall is active.
func UfwActive(out string) bool {
	return strings.Contains(strings.ToLower(out), "status: active")
}

// FirewalldRunning reports whether `firewall-cmd --state` says "running".
func FirewalldRunning(out string) bool {
	return strings.TrimSpace(out) == "running"
}

// ManageBdeProtected reports whether `manage-bde -status C:` shows protection on.
func ManageBdeProtected(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		l := strings.ToLower(strings.TrimSpace(line))
		if strings.HasPrefix(l, "protection status:") {
			return strings.Contains(l, "protection on")
		}
	}
	return false
}

// NetshAllOn reports whether `netsh advfirewall show allprofiles state` shows every profile ON.
func NetshAllOn(out string) bool {
	on, off := 0, 0
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) == 2 && strings.EqualFold(f[0], "state") {
			if strings.EqualFold(f[1], "on") {
				on++
			} else {
				off++
			}
		}
	}
	return on > 0 && off == 0
}

// RegScreenSaverSecure reports whether `reg query "HKCU\Control Panel\Desktop" /v ScreenSaverIsSecure`
// shows the value 1.
func RegScreenSaverSecure(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) == 3 && strings.EqualFold(f[0], "ScreenSaverIsSecure") {
			return f[2] == "1"
		}
	}
	return false
}

// FileVaultOn reports whether `fdesetup status` says FileVault is on.
func FileVaultOn(out string) bool {
	return strings.Contains(out, "FileVault is On")
}

// MacFirewallOn reports whether `socketfilterfw --getglobalstate` says the firewall is enabled.
func MacFirewallOn(out string) bool {
	return strings.Contains(strings.ToLower(out), "firewall is enabled")
}

// MacScreenLock reads `sysadminctl -screenLock status`: "screenLock delay is immediate" or
// "screenLock is off". The second return is false when the answer is not understood.
func MacScreenLock(out string) (on bool, known bool) {
	l := strings.ToLower(out)
	switch {
	case strings.Contains(l, "screenlock is off"):
		return false, true
	case strings.Contains(l, "screenlock delay is"):
		return true, true
	}
	return false, false
}

// DaysSince returns the whole days from t to now, never negative.
func DaysSince(t, now time.Time) int {
	d := int(now.Sub(t).Hours() / 24)
	if d < 0 {
		return 0
	}
	return d
}

var macHistoryDate = regexp.MustCompile(`(\d{1,2})/(\d{1,2})/(\d{4}), \d{1,2}:\d{2}:\d{2}`)

// MacLastUpdate finds the newest date in `softwareupdate --history`, and whether there was one.
func MacLastUpdate(out string) (time.Time, bool) {
	var newest time.Time
	for _, m := range macHistoryDate.FindAllStringSubmatch(out, -1) {
		mo, _ := strconv.Atoi(m[1])
		d, _ := strconv.Atoi(m[2])
		y, _ := strconv.Atoi(m[3])
		t := time.Date(y, time.Month(mo), d, 0, 0, 0, 0, time.UTC)
		if t.After(newest) {
			newest = t
		}
	}
	return newest, !newest.IsZero()
}

// IsoDate parses the first yyyy-mm-dd in s.
func IsoDate(s string) (time.Time, bool) {
	m := regexp.MustCompile(`\d{4}-\d{2}-\d{2}`).FindString(s)
	if m == "" {
		return time.Time{}, false
	}
	t, err := time.Parse("2006-01-02", m)
	return t, err == nil
}
