// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package ipvs

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const conf = `
global_defs {
    router_id se-a
}

# UDP service dns
virtual_server 192.0.2.22 53 {
    delay_loop 4
    lb_algo wlc
    lb_kind NAT
    protocol UDP
    real_server 10.20.0.40 53 {
        weight 100
        UDP_CHECK {
            retry 4
        }
    }
    real_server 10.20.0.41 53 {
        weight 50
    }
    real_server 10.20.0.42 53 {
        weight 0
    }
}

# UDP service web
virtual_server 192.0.2.30 80 {
    protocol TCP
    real_server 10.20.0.99 80 {
        weight 1
    }
}
`

const listing = `IP Virtual Server version 1.2.1 (size=4096)
Prot LocalAddress:Port Scheduler Flags
  -> RemoteAddress:Port           Forward Weight ActiveConn InActConn
UDP  192.0.2.22:53 wlc
  -> 10.20.0.40:53                Masq    100    3          7
  -> 10.20.0.42:53                Masq    0      1          0
`

const stats = `IP Virtual Server version 1.2.1 (size=4096)
Prot LocalAddress:Port                 Conns   InPkts  OutPkts  InBytes OutBytes
  -> RemoteAddress:Port
UDP  192.0.2.22:53                          10       20       30     4000     5000
  -> 10.20.0.40:53                          10       20       30     4000     5000
  -> 10.20.0.42:53                           1        2        3      400      500
`

func TestParseConfigKeepsOnlyUDPRealServers(t *testing.T) {
	got := ParseConfig([]byte(conf))
	if len(got) != 3 {
		t.Fatalf("want the 3 dns servers, got %v", got)
	}
	if got[0].Service != "dns" || got[0].VIP != "192.0.2.22" || got[0].Port != 53 || got[0].Weight != 100 {
		t.Fatalf("unexpected first server %+v", got[0])
	}
	if got[1].Weight != 50 || got[2].Weight != 0 {
		t.Fatalf("weights not read: %+v", got)
	}
}

func TestMergeStates(t *testing.T) {
	merged := Merge(ParseConfig([]byte(conf)), ParseTable([]byte(listing), []byte(stats)))
	if len(merged) != 3 {
		t.Fatalf("%v", merged)
	}
	up, down, drain := merged[0], merged[1], merged[2]
	if up.State != "up" || up.ActiveConns != 3 || up.InactiveConns != 7 || up.InPackets != 20 ||
		up.OutPackets != 30 || up.InBytes != 4000 || up.OutBytes != 5000 {
		t.Fatalf("up server: %v", up)
	}
	if down.Address != "10.20.0.41" || down.State != "down" || down.Weight != 50 {
		t.Fatalf("a configured server missing from the table is down: %v", down)
	}
	if drain.State != "drain" || drain.Weight != 0 {
		t.Fatalf("weight 0 drains: %v", drain)
	}
}

func TestParseIPv6(t *testing.T) {
	table := ParseTable([]byte("UDP  [2001:db8::5]:53 rr\n  -> [2001:db8:1::9]:53    Masq    100    0          0\n"), nil)
	e := table[key{"2001:db8::5", 53, "2001:db8:1::9", 53}]
	if e == nil || e.Weight != 100 {
		t.Fatalf("%v", table)
	}
}

type fake map[string]string

func (f fake) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	return []byte(f[name+" "+strings.Join(args, " ")]), nil
}

func TestCollectReadsConfigAndTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "keepalived.conf")
	if err := os.WriteFile(path, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	run := fake{"ipvsadm -Ln --exact": listing, "ipvsadm -Ln --stats --exact": stats}
	got, err := Collect(context.Background(), run, path)
	if err != nil || len(got) != 3 {
		t.Fatalf("%v %v", got, err)
	}
	none, err := Collect(context.Background(), run, filepath.Join(t.TempDir(), "missing"))
	if err != nil || none != nil {
		t.Fatalf("a missing file means no UDP services: %v %v", none, err)
	}
}

func TestUnhealthyVIPs(t *testing.T) {
	servers := Merge(ParseConfig([]byte(conf)), ParseTable([]byte(listing), []byte(stats)))
	if bad := UnhealthyVIPs(servers, true); len(bad) != 0 {
		t.Fatalf("one server is up: %v", bad)
	}
	if bad := UnhealthyVIPs(servers, false); !bad["192.0.2.22"] {
		t.Fatalf("keepalived down must flag the VIP: %v", bad)
	}
	none := Merge(ParseConfig([]byte(conf)), ParseTable(nil, nil))
	if bad := UnhealthyVIPs(none, true); !bad["192.0.2.22"] {
		t.Fatalf("no server up must flag the VIP: %v", bad)
	}
}

// Direct-return ("Route") and tunnel ("Tunnel") real servers list the same way as NAT ones (ADR 0192).
func TestParseTableDirectReturnAndTunnel(t *testing.T) {
	listing := "UDP  192.0.2.22:5004 rr\n  -> 10.20.0.60:5004   Route   100    2          1\n" +
		"UDP  192.0.2.23:5005 rr\n  -> 10.99.4.10:5005   Tunnel  50     0          0\n"
	table := ParseTable([]byte(listing), nil)
	dr := table[key{"192.0.2.22", 5004, "10.20.0.60", 5004}]
	tun := table[key{"192.0.2.23", 5005, "10.99.4.10", 5005}]
	if dr == nil || dr.Weight != 100 || dr.Active != 2 || dr.Inactive != 1 {
		t.Fatalf("direct return entry wrong: %+v", dr)
	}
	if tun == nil || tun.Weight != 50 {
		t.Fatalf("tunnel entry wrong: %+v", tun)
	}
}
