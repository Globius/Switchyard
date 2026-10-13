// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package status

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParseBGP(t *testing.T) {
	out, err := ParseBGP([]byte(`{"ipv4Unicast":{"peers":{"10.0.0.9":{"remoteAs":65002,"state":"Active"},"10.0.0.2":{"remoteAs":65001,"state":"Established"}}}}`))
	if err != nil || len(out) != 2 {
		t.Fatalf("%v %v", out, err)
	}
	if out[0].Peer != "10.0.0.2" || out[0].State != "Established" || out[1].State != "Active" {
		t.Fatalf("not sorted by peer: %v", out)
	}
	both, err := ParseBGP([]byte(`{"ipv4Unicast":{"peers":{"10.0.0.2":{"remoteAs":65001,"state":"Established"}}},"ipv6Unicast":{"peers":{"fd30::fa":{"remoteAs":65000,"state":"Established"},"10.0.0.2":{"remoteAs":65001,"state":"Active"}}}}`))
	if err != nil || len(both) != 2 || both[0].Peer != "10.0.0.2" || both[0].State != "Established" || both[1].Peer != "fd30::fa" {
		t.Fatalf("both families must be reported once per neighbor: %v %v", both, err)
	}
	if _, err := ParseBGP([]byte("nope")); err == nil {
		t.Fatal("garbage must fail")
	}
}

func TestVRRPIgnoresOddNames(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "vrrp-edge.state"), []byte("backup"), 0o600)
	os.WriteFile(filepath.Join(dir, "vrrp-Bad Name.state"), []byte("master"), 0o600)
	got := VRRP(dir)
	if len(got) != 1 || got[0].Name != "edge" || got[0].State != "BACKUP" {
		t.Fatalf("%v", got)
	}
}

type ipvsRunner map[string]string

func (r ipvsRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	return []byte(r[name+" "+strings.Join(args, " ")]), nil
}

func TestCollectReportsIPVSWhenUDPIsConfigured(t *testing.T) {
	conf := filepath.Join(t.TempDir(), "keepalived.conf")
	os.WriteFile(conf, []byte("# UDP service dns\nvirtual_server 192.0.2.22 53 {\n protocol UDP\n real_server 10.20.0.40 53 {\n  weight 100\n }\n}\n"), 0o600)
	run := ipvsRunner{
		"ipvsadm -Ln --exact":         "UDP  192.0.2.22:53 rr\n  -> 10.20.0.40:53   Masq   100  2  1\n",
		"ipvsadm -Ln --stats --exact": "UDP  192.0.2.22:53  1 2 3 4 5\n  -> 10.20.0.40:53   1 2 3 4 5\n",
	}
	c := &Collector{Runner: run, KeepalivedConf: conf, StateDir: t.TempDir(), Now: time.Now}
	st := c.Collect(context.Background())
	if len(st.Ipvs) != 1 || st.Ipvs[0].State != "up" || st.Ipvs[0].Service != "dns" || !st.KeepalivedRunning {
		t.Fatalf("%v running=%v", st.Ipvs, st.KeepalivedRunning)
	}
	c.KeepalivedConf = filepath.Join(t.TempDir(), "absent")
	if got := c.Collect(context.Background()); len(got.Ipvs) != 0 {
		t.Fatalf("no UDP config means no IPVS report: %v", got.Ipvs)
	}
}

type failingRunner struct{ err error }

func (r failingRunner) Run(context.Context, string, ...string) ([]byte, error) { return nil, r.err }

func TestCollectReportsTheWafSidecar(t *testing.T) {
	conf := filepath.Join(t.TempDir(), "config.yaml")
	c := &Collector{Runner: ipvsRunner{}, WafConfig: conf, WafProbe: func() bool { return true }, StateDir: t.TempDir(), Now: time.Now}
	if st := c.Collect(context.Background()); st.WafExpected || st.WafRunning {
		t.Fatalf("no Coraza config means no WAF is expected: %v %v", st.WafExpected, st.WafRunning)
	}
	os.WriteFile(conf, []byte("bind: 127.0.0.1:9000\n"), 0o600)
	if st := c.Collect(context.Background()); !st.WafExpected || !st.WafRunning {
		t.Fatalf("active unit and open port: expected=%v running=%v", st.WafExpected, st.WafRunning)
	}
	c.WafProbe = func() bool { return false }
	if st := c.Collect(context.Background()); !st.WafExpected || st.WafRunning {
		t.Fatalf("a closed port means the sidecar is not serving: %v", st.WafRunning)
	}
	c.WafProbe = func() bool { return true }
	c.Runner = failingRunner{err: os.ErrNotExist}
	if st := c.Collect(context.Background()); !st.WafExpected || st.WafRunning {
		t.Fatalf("an inactive unit is not running: %v", st.WafRunning)
	}
}
