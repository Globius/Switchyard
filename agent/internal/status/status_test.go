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
