// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package status collects what the agent reports to the controller.
package status

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/ipvs"
	"switchyard/agent/internal/runner"
)

// Version is set at build time.
var Version = "dev"

var vrrpName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Collector builds Status messages.
type Collector struct {
	Runner runner.Runner
	// StateDir holds one file per VRRP instance (vrrp-<name>.state) written by keepalived notify hooks.
	StateDir string
	Hashes   func() (map[string]string, error)
	BundleID func() string
	// Health reports the local HAProxy health and whether BGP prefixes are withdrawn.
	Health func() (healthy, withdrawn bool)
	// Withdrawn lists the BGP prefixes the watchdog has withdrawn.
	Withdrawn func() []string
	Now       func() time.Time
	// KeepalivedConf is the rendered keepalived.conf; its UDP virtual servers decide whether IPVS is reported.
	KeepalivedConf string
	// WafConfig is the Coraza configuration the controller renders; when it exists the WAF sidecar must run.
	WafConfig string
	// WafProbe reports whether the sidecar accepts connections (default: dial 127.0.0.1:9000).
	WafProbe func() bool
}

type bgpPeers struct {
	Peers map[string]struct {
		RemoteAs uint32 `json:"remoteAs"`
		State    string `json:"state"`
	} `json:"peers"`
}

// bgpSummary has one block per address family; a neighbor shows up in the families it is activated for.
type bgpSummary struct {
	IPv4Unicast bgpPeers `json:"ipv4Unicast"`
	IPv6Unicast bgpPeers `json:"ipv6Unicast"`
}

// ParseBGP turns `vtysh -c "show bgp summary json"` output into sessions sorted by peer. Sessions of both address
// families are reported (an IPv6-only engine has only IPv6 neighbors); a neighbor in both appears once, as seen under IPv4.
func ParseBGP(data []byte) ([]*pb.BgpSession, error) {
	var s bgpSummary
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, err
	}
	var out []*pb.BgpSession
	seen := map[string]bool{}
	for _, family := range []bgpPeers{s.IPv4Unicast, s.IPv6Unicast} {
		for peer, p := range family.Peers {
			if !seen[peer] {
				seen[peer] = true
				out = append(out, &pb.BgpSession{Peer: peer, State: p.State, RemoteAsn: p.RemoteAs})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Peer < out[j].Peer })
	return out, nil
}

// VRRP reads the instance states written by keepalived notify hooks.
func VRRP(dir string) []*pb.VrrpInstance {
	matches, _ := filepath.Glob(filepath.Join(dir, "vrrp-*.state"))
	sort.Strings(matches)
	var out []*pb.VrrpInstance
	for _, m := range matches {
		name := strings.TrimSuffix(strings.TrimPrefix(filepath.Base(m), "vrrp-"), ".state")
		data, err := os.ReadFile(m)
		if err != nil || !vrrpName.MatchString(name) {
			continue
		}
		out = append(out, &pb.VrrpInstance{Name: name, State: strings.ToUpper(strings.TrimSpace(string(data)))})
	}
	return out
}

// Collect gathers a status snapshot. A source that fails is simply absent from the result.
func (c *Collector) Collect(ctx context.Context) *pb.Status {
	st := &pb.Status{AgentVersion: Version, ObservedAtUnix: c.Now().Unix()}
	if c.BundleID != nil {
		st.BundleId = c.BundleID()
	}
	if c.Hashes != nil {
		if h, err := c.Hashes(); err == nil {
			st.FileHashes = h
		}
	}
	if c.Runner != nil {
		if out, err := c.Runner.Run(ctx, "vtysh", "-c", "show bgp summary json"); err == nil {
			if sessions, perr := ParseBGP(out); perr == nil {
				st.Bgp = sessions
			}
		}
	}
	st.Vrrp = VRRP(c.StateDir)
	if c.Runner != nil && c.KeepalivedConf != "" {
		if servers, err := ipvs.Collect(ctx, c.Runner, c.KeepalivedConf); err == nil && len(servers) > 0 {
			st.Ipvs = servers
			_, aerr := c.Runner.Run(ctx, "systemctl", "is-active", "--quiet", "keepalived")
			st.KeepalivedRunning = aerr == nil
		}
	}
	if c.WafConfig != "" {
		if _, err := os.Stat(c.WafConfig); err == nil {
			st.WafExpected = true
			st.WafRunning = c.wafRunning(ctx)
		}
	}
	if c.Health != nil {
		st.HaproxyHealthy, st.RoutesWithdrawn = c.Health()
	}
	if c.Withdrawn != nil {
		st.WithdrawnPrefixes = c.Withdrawn()
	}
	return st
}

// wafRunning is true when the coraza-spoa unit is active and its port answers: the unit can be "active" while the
// agent still refuses connections (starting, crash-looping with restarts), and HAProxy needs the port.
func (c *Collector) wafRunning(ctx context.Context) bool {
	if c.Runner == nil {
		return false
	}
	if _, err := c.Runner.Run(ctx, "systemctl", "is-active", "--quiet", "coraza-spoa"); err != nil {
		return false
	}
	if c.WafProbe != nil {
		return c.WafProbe()
	}
	conn, err := net.DialTimeout("tcp", "127.0.0.1:9000", time.Second)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}
