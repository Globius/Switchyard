// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package ipvs reports the state of the IPVS real servers behind UDP virtual services.
//
// Keepalived owns the IPVS table: it adds a real server when its check passes and removes it when
// the check fails. The agent therefore compares the table (read with ipvsadm) with the real
// servers in the rendered keepalived.conf: a configured server missing from the table is down, one
// with weight 0 is draining. The result has the same shape as an HAProxy member (state, weight,
// connections, counters), so nothing downstream needs a UDP-specific branch.
package ipvs

import (
	"context"
	"math"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/runner"
)

// Server is one real server of a virtual service as configured in keepalived.conf.
type Server struct {
	Service string
	VIP     string
	Port    uint32
	Addr    string
	AddrPrt uint32
	Weight  uint32
}

// Entry is one real server in the kernel table, with its connection counts and counters.
type Entry struct {
	Weight                     uint32
	Active, Inactive           uint64
	InPkts, OutPkts            uint64
	InBytes, OutBytes          uint64
	seenListing, seenStatistic bool
}

type key struct {
	vip  string
	port uint32
	addr string
	prt  uint32
}

var (
	serviceComment = regexp.MustCompile(`^#\s*UDP service (\S+)$`)
	virtualServer  = regexp.MustCompile(`^virtual_server\s+(\S+)\s+(\d+)\s*\{$`)
	realServer     = regexp.MustCompile(`^real_server\s+(\S+)\s+(\d+)\s*\{$`)
	weightLine     = regexp.MustCompile(`^weight\s+(\d+)$`)
	tableVirtual   = regexp.MustCompile(`^UDP\s+(\S+)((?:\s+\S+)*)$`)
	tableReal      = regexp.MustCompile(`^->\s+(\S+)((?:\s+\S+)*)$`)
)

// hostPort splits "10.0.0.1:53" or "[2001:db8::1]:53".
func hostPort(s string) (string, uint32, bool) {
	i := strings.LastIndex(s, ":")
	if i < 0 {
		return "", 0, false
	}
	port, err := strconv.ParseUint(s[i+1:], 10, 32)
	if err != nil {
		return "", 0, false
	}
	return strings.Trim(s[:i], "[]"), uint32(port), true
}

// ParseConfig returns the real servers of the UDP virtual servers in a keepalived.conf.
func ParseConfig(conf []byte) []Server {
	type vs struct {
		service string
		vip     string
		port    uint32
		udp     bool
		servers []Server
	}
	var all []*vs
	var cur *vs
	var rs *Server
	service := ""
	for _, raw := range strings.Split(string(conf), "\n") {
		line := strings.TrimSpace(raw)
		if m := serviceComment.FindStringSubmatch(line); m != nil {
			service = m[1]
			continue
		}
		if m := virtualServer.FindStringSubmatch(line); m != nil {
			port, _ := strconv.ParseUint(m[2], 10, 32)
			cur = &vs{service: service, vip: m[1], port: uint32(port)}
			all = append(all, cur)
			rs = nil
			continue
		}
		if cur == nil {
			continue
		}
		if line == "protocol UDP" {
			cur.udp = true
		} else if m := realServer.FindStringSubmatch(line); m != nil {
			port, _ := strconv.ParseUint(m[2], 10, 32)
			cur.servers = append(cur.servers, Server{
				Service: cur.service, VIP: cur.vip, Port: cur.port,
				Addr: m[1], AddrPrt: uint32(port), Weight: 100,
			})
			rs = &cur.servers[len(cur.servers)-1]
		} else if m := weightLine.FindStringSubmatch(line); m != nil && rs != nil {
			w, _ := strconv.ParseUint(m[1], 10, 32)
			rs.Weight = uint32(w)
			rs = nil // only the first weight line belongs to the real server
		}
	}
	var out []Server
	for _, v := range all {
		if v.udp {
			out = append(out, v.servers...)
		}
	}
	return out
}

func fields(rest string) []uint64 {
	var out []uint64
	for _, f := range strings.Fields(rest) {
		n, err := strconv.ParseUint(f, 10, 64)
		if err != nil {
			return nil
		}
		out = append(out, n)
	}
	return out
}

// ParseTable merges `ipvsadm -Ln --exact` (weights and connections) and
// `ipvsadm -Ln --stats --exact` (packet and byte counters) into one entry per real server.
func ParseTable(listing, stats []byte) map[key]*Entry {
	table := map[key]*Entry{}
	walk := func(data []byte, apply func(*Entry, []string)) {
		var vip string
		var vport uint32
		for _, raw := range strings.Split(string(data), "\n") {
			line := strings.TrimSpace(raw)
			if m := tableVirtual.FindStringSubmatch(line); m != nil {
				if h, p, ok := hostPort(m[1]); ok {
					vip, vport = h, p
				}
				continue
			}
			m := tableReal.FindStringSubmatch(line)
			if m == nil || vip == "" {
				continue
			}
			addr, port, ok := hostPort(m[1])
			if !ok {
				continue
			}
			k := key{vip, vport, addr, port}
			if table[k] == nil {
				table[k] = &Entry{}
			}
			apply(table[k], strings.Fields(m[2]))
		}
	}
	walk(listing, func(e *Entry, f []string) { // Forward Weight ActiveConn InActConn
		n := fields(strings.Join(f[min(1, len(f)):], " "))
		if len(n) == 3 && n[0] <= math.MaxUint32 { // a weight beyond uint32 is malformed; skip the line
			e.Weight, e.Active, e.Inactive, e.seenListing = uint32(n[0]), n[1], n[2], true
		}
	})
	walk(stats, func(e *Entry, f []string) { // Conns InPkts OutPkts InBytes OutBytes
		n := fields(strings.Join(f, " "))
		if len(n) == 5 {
			e.InPkts, e.OutPkts, e.InBytes, e.OutBytes, e.seenStatistic = n[1], n[2], n[3], n[4], true
		}
	})
	return table
}

// Merge combines the configured real servers with the kernel table.
func Merge(configured []Server, table map[key]*Entry) []*pb.IpvsRealServer {
	out := make([]*pb.IpvsRealServer, 0, len(configured))
	for _, c := range configured {
		r := &pb.IpvsRealServer{
			Service: c.Service, Vip: c.VIP, Port: c.Port, Address: c.Addr, MemberPort: c.AddrPrt,
			State: "down", Weight: c.Weight,
		}
		if e, ok := table[key{c.VIP, c.Port, c.Addr, c.AddrPrt}]; ok {
			r.Weight, r.ActiveConns, r.InactiveConns = e.Weight, e.Active, e.Inactive
			r.InPackets, r.OutPackets, r.InBytes, r.OutBytes = e.InPkts, e.OutPkts, e.InBytes, e.OutBytes
			r.State = "up"
			if e.Weight == 0 {
				r.State = "drain"
			}
		} else if c.Weight == 0 {
			r.State = "drain"
		}
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Vip != b.Vip {
			return a.Vip < b.Vip
		}
		if a.Port != b.Port {
			return a.Port < b.Port
		}
		if a.Address != b.Address {
			return a.Address < b.Address
		}
		return a.MemberPort < b.MemberPort
	})
	return out
}

// Collect reads keepalived.conf at confPath and the kernel table. It returns nothing when the
// file is missing or has no UDP virtual server, and an error when ipvsadm cannot be run.
func Collect(ctx context.Context, run runner.Runner, confPath string) ([]*pb.IpvsRealServer, error) {
	conf, err := os.ReadFile(confPath)
	if err != nil {
		return nil, nil
	}
	configured := ParseConfig(conf)
	if len(configured) == 0 {
		return nil, nil
	}
	listing, err := run.Run(ctx, "ipvsadm", "-Ln", "--exact")
	if err != nil {
		return nil, err
	}
	stats, err := run.Run(ctx, "ipvsadm", "-Ln", "--stats", "--exact")
	if err != nil {
		return nil, err
	}
	return Merge(configured, ParseTable(listing, stats)), nil
}

// UnhealthyVIPs returns the virtual addresses that cannot serve: keepalived is not running, or a
// virtual service on the address has no real server that is up. The agent watchdog withdraws these
// BGP prefixes, the way it does when the local HAProxy is unhealthy.
func UnhealthyVIPs(servers []*pb.IpvsRealServer, keepalivedRunning bool) map[string]bool {
	bad := map[string]bool{}
	up := map[[2]string]bool{}
	for _, s := range servers {
		k := [2]string{s.Vip, strconv.Itoa(int(s.Port))}
		if _, seen := up[k]; !seen {
			up[k] = false
		}
		if s.State == "up" {
			up[k] = true
		}
	}
	for k, ok := range up {
		if !ok || !keepalivedRunning {
			bad[k[0]] = true
		}
	}
	return bad
}
