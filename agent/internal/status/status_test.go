package status

import (
	"os"
	"path/filepath"
	"testing"
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
