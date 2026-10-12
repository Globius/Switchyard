package bundle

import (
	"context"
	"crypto/ed25519"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "switchyard/agent/gen/switchyard/agent/v1"
)

type fakeRunner struct {
	mu    sync.Mutex
	calls []string
	fail  map[string]bool
}

func (f *fakeRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	call := name + " " + strings.Join(args, " ")
	f.calls = append(f.calls, call)
	if f.fail[call] {
		f.fail[call] = false // fail once, like a transient reload error
		return []byte("boom"), errors.New("exit 1")
	}
	return nil, nil
}

func setup(t *testing.T) (*Applier, ed25519.PrivateKey, *fakeRunner) {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(nil)
	r := &fakeRunner{fail: map[string]bool{}}
	return &Applier{Root: t.TempDir(), PubKey: pub, Runner: r, AllowUnbound: true}, priv, r
}

func request(t *testing.T, priv ed25519.PrivateKey, id string, files map[string]string) *pb.ApplyBundleRequest {
	t.Helper()
	b := &pb.Bundle{Id: id}
	for p, c := range files {
		b.Files = append(b.Files, &pb.File{Path: p, Content: []byte(c)})
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	return &pb.ApplyBundleRequest{Bundle: raw, Signature: ed25519.Sign(priv, raw)}
}

func read(t *testing.T, a *Applier, rel string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(a.Root, filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestApplyWritesFilesAndReloadsOnlyChangedUnits(t *testing.T) {
	a, priv, r := setup(t)
	resp := a.Apply(context.Background(), request(t, priv, "b1", map[string]string{
		"frr/frr.conf": "router bgp 1\n", "keepalived/keepalived.conf": "vrrp\n",
	}))
	if !resp.Ok {
		t.Fatal(resp.Error)
	}
	if !reflect.DeepEqual(resp.ReloadedUnits, []string{"frr", "keepalived"}) {
		t.Fatalf("units: %v", resp.ReloadedUnits)
	}
	if read(t, a, "frr/frr.conf") != "router bgp 1\n" || a.LastBundleID() != "b1" {
		t.Fatal("files or bundle id not stored")
	}

	r.calls = nil
	resp = a.Apply(context.Background(), request(t, priv, "b2", map[string]string{
		"frr/frr.conf": "router bgp 2\n", "keepalived/keepalived.conf": "vrrp\n",
	}))
	if !resp.Ok || !reflect.DeepEqual(resp.ReloadedUnits, []string{"frr"}) {
		t.Fatalf("only frr changed: %+v", resp)
	}
	if !reflect.DeepEqual(r.calls, []string{"systemctl is-active --quiet frr", "systemctl reload frr"}) {
		t.Fatalf("calls: %v", r.calls)
	}
}

func TestUnchangedBundleReloadsNothing(t *testing.T) {
	a, priv, r := setup(t)
	files := map[string]string{"frr/frr.conf": "x\n"}
	a.Apply(context.Background(), request(t, priv, "b1", files))
	r.calls = nil
	resp := a.Apply(context.Background(), request(t, priv, "b1", files))
	if !resp.Ok || len(resp.ReloadedUnits) != 0 || len(r.calls) != 0 || len(resp.ChangedFiles) != 0 {
		t.Fatalf("expected a no-op, got %+v calls=%v", resp, r.calls)
	}
}

func TestFilesAbsentFromTheBundleAreRemoved(t *testing.T) {
	a, priv, _ := setup(t)
	a.Apply(context.Background(), request(t, priv, "b1", map[string]string{"frr/a.conf": "1", "frr/b.conf": "2"}))
	resp := a.Apply(context.Background(), request(t, priv, "b2", map[string]string{"frr/a.conf": "1"}))
	if !resp.Ok || !reflect.DeepEqual(resp.ChangedFiles, []string{"frr/b.conf"}) {
		t.Fatalf("%+v", resp)
	}
	if _, err := os.Stat(filepath.Join(a.Root, "frr", "b.conf")); !os.IsNotExist(err) {
		t.Fatal("b.conf should be gone")
	}
}

func TestBadSignatureAndTamperingAreRejected(t *testing.T) {
	a, priv, r := setup(t)
	req := request(t, priv, "b1", map[string]string{"frr/frr.conf": "x"})
	req.Bundle = append(req.Bundle, 0)
	if resp := a.Apply(context.Background(), req); resp.Ok {
		t.Fatal("tampered bundle accepted")
	}
	_, other, _ := ed25519.GenerateKey(nil)
	if resp := a.Apply(context.Background(), request(t, other, "b1", map[string]string{"frr/frr.conf": "x"})); resp.Ok {
		t.Fatal("bundle signed by the wrong key accepted")
	}
	if len(r.calls) != 0 {
		t.Fatal("nothing may run for a rejected bundle")
	}
	if _, err := os.Stat(filepath.Join(a.Root, "frr")); !os.IsNotExist(err) {
		t.Fatal("nothing may be written for a rejected bundle")
	}
}

func TestPathsOutsideTheManagedDirectoriesAreRejected(t *testing.T) {
	a, priv, _ := setup(t)
	bad := []string{
		"../etc/passwd", "/etc/passwd", "frr/../../x", "haproxy/haproxy.cfg", "frr", "frr/",
		"agent/tls.key", "frr//x", "frr/./x", "frr\\x", "systemd/system/x.service",
	}
	for _, p := range bad {
		resp := a.Apply(context.Background(), request(t, priv, "b", map[string]string{p: "x"}))
		if resp.Ok {
			t.Errorf("path %q was accepted", p)
		}
	}
	if err := ValidatePath("frr/sub/x.conf"); err != nil {
		t.Errorf("a nested managed path must be fine: %v", err)
	}
}

func TestLimits(t *testing.T) {
	a, priv, _ := setup(t)
	big := strings.Repeat("x", maxFileSize+1)
	if resp := a.Apply(context.Background(), request(t, priv, "b", map[string]string{"frr/x": big})); resp.Ok {
		t.Fatal("oversized file accepted")
	}
	many := map[string]string{}
	for i := 0; i <= maxFiles; i++ {
		many["frr/f"+string(rune('a'+i%26))+strings.Repeat("x", i)] = "1"
	}
	if resp := a.Apply(context.Background(), request(t, priv, "b", many)); resp.Ok {
		t.Fatal("too many files accepted")
	}
}

func TestFailedReloadRestoresThePreviousFiles(t *testing.T) {
	a, priv, r := setup(t)
	a.Apply(context.Background(), request(t, priv, "b1", map[string]string{"frr/frr.conf": "good\n"}))
	r.calls = nil
	r.fail["systemctl reload frr"] = true
	resp := a.Apply(context.Background(), request(t, priv, "b2", map[string]string{"frr/frr.conf": "bad\n"}))
	if resp.Ok || !resp.RestoredPrevious {
		t.Fatalf("expected a restore: %+v", resp)
	}
	if read(t, a, "frr/frr.conf") != "good\n" {
		t.Fatal("previous file not restored")
	}
	if a.LastBundleID() != "b1" {
		t.Fatalf("bundle id must stay b1, got %s", a.LastBundleID())
	}
	if !reflect.DeepEqual(r.calls, []string{
		"systemctl is-active --quiet frr", "systemctl reload frr",
		"systemctl is-active --quiet frr", "systemctl reload frr",
	}) {
		t.Fatalf("expected a reload to apply and one to restore: %v", r.calls)
	}
}

func TestWritesAreAtomicAndLeaveNoTemporaryFiles(t *testing.T) {
	a, priv, _ := setup(t)
	a.Apply(context.Background(), request(t, priv, "b1", map[string]string{"frr/frr.conf": "x"}))
	entries, _ := os.ReadDir(filepath.Join(a.Root, "frr"))
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".sy-") {
			t.Fatalf("leftover temp file %s", e.Name())
		}
	}
	h, err := a.Hashes()
	if err != nil || h["frr/frr.conf"] != Hash([]byte("x")) {
		t.Fatalf("hashes: %v %v", h, err)
	}
}

func TestClientCAFilesAreManagedAndKeepOwnerOnlyPermissions(t *testing.T) {
	a, priv, _ := setup(t)
	files := map[string]string{"certs/ca-partners.pem": "CA", "certs/ca-partners.crl": "CRL"}
	resp := a.Apply(context.Background(), request(t, priv, "ca1", files))
	if !resp.Ok {
		t.Fatalf("a client CA bundle with a CRL was refused: %s", resp.Error)
	}
	if got := read(t, a, "certs/ca-partners.crl"); got != "CRL" {
		t.Errorf("crl not written: %q", got)
	}
	if info, _ := os.Stat(filepath.Join(a.Root, "certs", "ca-partners.crl")); runtimeIsUnix() && info.Mode().Perm() != 0o600 {
		t.Errorf("crl mode %v, want 0600", info.Mode().Perm())
	}
	resp = a.Apply(context.Background(), request(t, priv, "ca2", map[string]string{"frr/frr.conf": "x"}))
	if !resp.Ok {
		t.Fatalf("apply failed: %s", resp.Error)
	}
	if _, err := os.Stat(filepath.Join(a.Root, "certs", "ca-partners.crl")); err == nil {
		t.Error("the CRL should have been removed with the bundle that dropped it")
	}
}

func TestCertsAreManagedOnlyAsFlatPemFilesAndCrtListsAreLeftAlone(t *testing.T) {
	a, priv, r := setup(t)
	if err := os.MkdirAll(filepath.Join(a.Root, "certs"), 0o750); err != nil {
		t.Fatal(err)
	}
	crtlist := filepath.Join(a.Root, "certs", "web.crtlist")
	if err := os.WriteFile(crtlist, []byte("list"), 0o640); err != nil {
		t.Fatal(err)
	}
	resp := a.Apply(context.Background(), request(t, priv, "c1", map[string]string{"certs/site.pem": "KEY"}))
	if !resp.Ok {
		t.Fatalf("apply failed: %s", resp.Error)
	}
	if got := read(t, a, "certs/site.pem"); got != "KEY" {
		t.Errorf("pem not written: %q", got)
	}
	if !reflect.DeepEqual(resp.ReloadedUnits, []string{"haproxy"}) {
		t.Errorf("reloaded %v, want haproxy", resp.ReloadedUnits)
	}
	if info, _ := os.Stat(filepath.Join(a.Root, "certs", "site.pem")); runtimeIsUnix() && info.Mode().Perm() != 0o600 {
		t.Errorf("pem mode %v, want 0600", info.Mode().Perm())
	}
	// A later bundle without the pem removes it, but the crt-list stays.
	resp = a.Apply(context.Background(), request(t, priv, "c2", map[string]string{"frr/frr.conf": "x"}))
	if !resp.Ok {
		t.Fatalf("apply failed: %s", resp.Error)
	}
	if _, err := os.Stat(filepath.Join(a.Root, "certs", "site.pem")); err == nil {
		t.Error("pem should have been removed")
	}
	if _, err := os.Stat(crtlist); err != nil {
		t.Errorf("the crt-list must be untouched: %v", err)
	}
	for _, bad := range []string{"certs/web.crtlist", "certs/sub/x.pem", "certs/x.key"} {
		resp := a.Apply(context.Background(), request(t, priv, "c3", map[string]string{bad: "x"}))
		if resp.Ok {
			t.Errorf("path %q was accepted", bad)
		}
	}
	_ = r
}

func runtimeIsUnix() bool { return filepath.Separator == '/' }

func TestUDPFilesUseTheirOwnUnits(t *testing.T) {
	a, priv, r := setup(t)
	r.fail["systemctl is-active --quiet keepalived"] = true // not running yet
	resp := a.Apply(context.Background(), request(t, priv, "b1", map[string]string{
		"keepalived/keepalived.conf":  "virtual_server 192.0.2.1 53 {\n}\n",
		"nftables/switchyard-udp.nft": "table ip sy_udp {}\n",
		"sysctl/switchyard-udp.conf":  "net.ipv4.ip_forward = 1\n",
		"frr/vips.json":               "{}\n",
	}))
	if !resp.Ok {
		t.Fatal(resp.Error)
	}
	want := []string{
		"systemctl is-active --quiet keepalived", "systemctl start keepalived",
		"systemctl restart nftables", "systemctl restart systemd-sysctl",
	}
	for _, call := range want {
		if !contains(r.calls, call) {
			t.Fatalf("missing %q in %v", call, r.calls)
		}
	}
	if contains(r.calls, "systemctl reload keepalived") {
		t.Fatalf("a stopped keepalived must be started, not reloaded: %v", r.calls)
	}
}

func TestRunningKeepalivedIsReloadedAndStoppedWhenItsFilesGo(t *testing.T) {
	a, priv, r := setup(t)
	a.Apply(context.Background(), request(t, priv, "b1", map[string]string{"keepalived/keepalived.conf": "a\n"}))
	r.calls = nil
	a.Apply(context.Background(), request(t, priv, "b2", map[string]string{"keepalived/keepalived.conf": "b\n"}))
	if !contains(r.calls, "systemctl reload keepalived") {
		t.Fatalf("an active keepalived is reloaded: %v", r.calls)
	}
	r.calls = nil
	resp := a.Apply(context.Background(), request(t, priv, "b3", map[string]string{"frr/frr.conf": "x\n"}))
	if !resp.Ok || !contains(r.calls, "systemctl stop keepalived") {
		t.Fatalf("removing the last keepalived file stops it: %v %v", resp, r.calls)
	}
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

func boundRequest(t *testing.T, priv ed25519.PrivateKey, id, engine string, seq uint64, files map[string]string) *pb.ApplyBundleRequest {
	t.Helper()
	b := &pb.Bundle{Id: id, Engine: engine, Sequence: seq}
	for p, c := range files {
		b.Files = append(b.Files, &pb.File{Path: p, Content: []byte(c)})
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	return &pb.ApplyBundleRequest{Bundle: raw, Signature: ed25519.Sign(priv, raw)}
}

func TestBundlesAreBoundToTheEngineAndMustBeNewer(t *testing.T) {
	a, priv, _ := setup(t)
	a.AllowUnbound = false
	a.Engine = "se-a"
	files := map[string]string{"frr/frr.conf": "one"}
	ctx := context.Background()

	if resp := a.Apply(ctx, boundRequest(t, priv, "b1", "se-a", 100, files)); !resp.Ok {
		t.Fatalf("a bound bundle was refused: %s", resp.Error)
	}
	// The same bundle again, and an older one, are replays.
	for _, seq := range []uint64{100, 99, 1} {
		resp := a.Apply(ctx, boundRequest(t, priv, "old", "se-a", seq, map[string]string{"frr/frr.conf": "old"}))
		if resp.Ok || !strings.Contains(resp.Error, "not newer") {
			t.Errorf("sequence %d: replay accepted or wrong error: %+v", seq, resp)
		}
	}
	if got := read(t, a, "frr/frr.conf"); got != "one" {
		t.Errorf("a refused bundle changed the files: %q", got)
	}
	// A bundle for another engine is refused even with a fresh sequence.
	resp := a.Apply(ctx, boundRequest(t, priv, "b2", "se-b", 200, files))
	if resp.Ok || !strings.Contains(resp.Error, "for engine") {
		t.Errorf("a bundle for another engine was accepted: %+v", resp)
	}
	// Unbound bundles are refused unless explicitly allowed.
	for _, req := range []*pb.ApplyBundleRequest{
		boundRequest(t, priv, "b3", "", 300, files),
		boundRequest(t, priv, "b4", "se-a", 0, files),
		request(t, priv, "b5", files),
	} {
		if resp := a.Apply(ctx, req); resp.Ok {
			t.Errorf("an unbound bundle was accepted: %+v", resp)
		}
	}
	// A newer sequence works, and the sequence survives a restart (it is on disk).
	if resp := a.Apply(ctx, boundRequest(t, priv, "b6", "se-a", 101, map[string]string{"frr/frr.conf": "two"})); !resp.Ok {
		t.Fatalf("a newer bundle was refused: %s", resp.Error)
	}
	restarted := &Applier{Root: a.Root, PubKey: a.PubKey, Runner: a.Runner, Engine: "se-a"}
	if resp := restarted.Apply(ctx, boundRequest(t, priv, "b7", "se-a", 101, files)); resp.Ok {
		t.Error("a replay after a restart was accepted")
	}
}

func TestStoppedFRRIsStartedNotReloaded(t *testing.T) {
	a, priv, r := setup(t)
	r.fail["systemctl is-active --quiet frr"] = true // not running yet: the image skips it until it has a config
	resp := a.Apply(context.Background(), request(t, priv, "b1", map[string]string{"frr/frr.conf": "router bgp 1\n"}))
	if !resp.Ok {
		t.Fatal(resp.Error)
	}
	if !contains(r.calls, "systemctl start frr") || contains(r.calls, "systemctl reload frr") {
		t.Fatalf("a stopped frr must be started, not reloaded: %v", r.calls)
	}
	r.calls = nil
	resp = a.Apply(context.Background(), request(t, priv, "b2", map[string]string{"keepalived/keepalived.conf": "v\n"}))
	if !resp.Ok || !contains(r.calls, "systemctl stop frr") {
		t.Fatalf("removing the last frr file stops it: %v %v", resp, r.calls)
	}
}

func TestAuthProxyFilesAreFlatCfgAndEnvFiles(t *testing.T) {
	a, priv, r := setup(t)
	resp := a.Apply(context.Background(), request(t, priv, "a1", map[string]string{
		"auth/web-sso.cfg": "http_address = \"127.0.0.1:14000\"\n",
		"auth/web-sso.env": "OAUTH2_PROXY_CLIENT_SECRET=x\n",
	}))
	if !resp.Ok {
		t.Fatal(resp.Error)
	}
	if !contains(r.calls, "systemctl restart sy-auth") {
		t.Fatalf("the proxies must be restarted to read a new config: %v", r.calls)
	}
	if info, _ := os.Stat(filepath.Join(a.Root, "auth", "web-sso.env")); runtimeIsUnix() && info.Mode().Perm() != 0o640 {
		t.Errorf("auth env mode %v, want 0640 (the directory is root:sy-auth 0750)", info.Mode().Perm())
	}
	if info, _ := os.Stat(filepath.Join(a.Root, "auth", "web-sso.cfg")); runtimeIsUnix() && info.Mode().Perm() != 0o640 {
		t.Errorf("auth config mode %v, want 0640", info.Mode().Perm())
	}
	for _, bad := range []string{"auth/web.txt", "auth/sub/web.cfg", "auth/sub/web.env"} {
		resp = a.Apply(context.Background(), request(t, priv, "a-bad", map[string]string{bad: "x"}))
		if resp.Ok {
			t.Errorf("%s must not be accepted", bad)
		}
	}
	resp = a.Apply(context.Background(), request(t, priv, "a2", map[string]string{"frr/frr.conf": "x"}))
	if !resp.Ok {
		t.Fatal(resp.Error)
	}
	if !contains(r.calls, "systemctl stop sy-auth") {
		t.Fatalf("dropping the last access policy must stop the proxies: %v", r.calls)
	}
}
