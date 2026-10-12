// Package bundle verifies and applies signed configuration bundles.
//
// A bundle is the complete set of files the controller wants under the managed directories. The
// agent verifies the Ed25519 signature, checks every path, writes atomically, reloads only the
// units whose files changed (from a fixed allowlist), and puts the previous files back if a reload
// fails.
package bundle

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"google.golang.org/protobuf/proto"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/runner"
)

const (
	maxFiles    = 200
	maxFileSize = 1 << 20
)

// Units maps a managed directory to the systemd unit that reloads when its files change. This is
// the allowlist: nothing else is ever reloaded.
var Units = map[string]string{
	"frr":        "frr",
	"keepalived": "keepalived",
	"coraza":     "coraza-spoa",
	"certs":      "haproxy",
	"nftables":   "nftables",
	"auth":       "sy-auth",
	"sysctl":     "systemd-sysctl",
}

// verbs overrides the systemctl verb for units that cannot reload (default "reload"). The
// sysctl directory is applied by restarting systemd-sysctl, which re-reads the settings. The
// nftables unit is a one-shot that is inactive until it has run once, and a reload of an inactive
// unit fails, so it is restarted (stop flushes the ruleset, start loads /etc/nftables.conf, which
// includes the managed files); conntrack keeps the NAT of flows already established.
var verbs = map[string]string{
	"systemd-sysctl": "restart",
	"nftables":       "restart",
	"sy-auth":        "restart", // oauth2-proxy does not reload its configuration
}

// onDemand units run only while they have files: they are started when the first file appears and stopped
// when the last one is gone. The SE image skips them at boot until their configuration exists, so an engine
// without that feature carries no idle daemon, and one that has it comes back after a reboot without the controller.
var onDemand = map[string]bool{"keepalived": true, "frr": true, "sy-auth": true, "coraza-spoa": true}

func verb(unit string) string {
	if v, ok := verbs[unit]; ok {
		return v
	}
	return "reload"
}

// suffixOnly restricts a managed directory to files with one of these suffixes. The Data Plane API
// keeps its crt-lists in the same directory as the certificates, so only "*.pem" files (server
// certificates and client CA bundles) and "*.crl" files (client CA revocation lists) there are the
// agent's; everything else is left alone and never removed.
var suffixOnly = map[string][]string{"certs": {".pem", ".crl", ".secret"}, "auth": {".cfg", ".env"}}

// secretDirs hold private keys and are written with owner-only permissions.
// The auth directory is not here: its files are group-readable (0640) because the proxies run as
// their own user and the SE image makes the directory root:sy-auth 0750, which is what keeps them
// private (including the "*.env" files that hold the secrets).
var secretDirs = map[string]bool{"certs": true}

// groupDirs name the group that may read a managed directory: the sidecar that runs from it. The
// agent writes the files 0640 and, when the group exists on the engine, hands them to it (and the
// directory with them), so the sidecar needs no other access to /etc/switchyard.
var groupDirs = map[string]string{"auth": "sy-auth", "coraza": "coraza"}

func managedFile(rel string) bool {
	prefix, rest, ok := strings.Cut(rel, "/")
	if !ok {
		return false
	}
	suffixes, restricted := suffixOnly[prefix]
	if !restricted {
		return true
	}
	if strings.Contains(rest, "/") {
		return false
	}
	for _, suffix := range suffixes {
		if strings.HasSuffix(rest, suffix) {
			return true
		}
	}
	return false
}

// Applier applies bundles under Root.
type Applier struct {
	Root   string
	PubKey ed25519.PublicKey
	Runner runner.Runner
	// Engine is the name this agent serves (its certificate name). When set, a bundle signed for
	// another engine is refused.
	Engine string
	// AllowUnbound accepts bundles without an engine or a sequence, from controllers older than
	// those fields. Off by default: an unbound bundle can be replayed or sent to another engine.
	AllowUnbound bool
}

const seqFile = "agent/bundle-seq"

// lastSequence returns the sequence of the last applied bundle, or 0.
func (a *Applier) lastSequence() uint64 {
	data, ok := a.read(seqFile)
	if !ok {
		return 0
	}
	n, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// checkBinding refuses a bundle that is for another engine or not newer than the last one applied.
func (a *Applier) checkBinding(b *pb.Bundle) error {
	if !a.AllowUnbound {
		if a.Engine != "" && b.Engine == "" {
			return errors.New("bundle is not bound to an engine")
		}
		if b.Sequence == 0 {
			return errors.New("bundle has no sequence number")
		}
	}
	if a.Engine != "" && b.Engine != "" && b.Engine != a.Engine {
		return fmt.Errorf("bundle is for engine %q, not %q", b.Engine, a.Engine)
	}
	if b.Sequence != 0 && b.Sequence <= a.lastSequence() {
		return fmt.Errorf("bundle sequence %d is not newer than the last applied (%d)", b.Sequence, a.lastSequence())
	}
	return nil
}

// Hash returns the hex SHA-256 of data.
func Hash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// ValidatePath rejects anything that is not a clean relative path inside a managed directory.
func ValidatePath(p string) error {
	if p == "" || strings.ContainsRune(p, 0) || strings.Contains(p, "\\") {
		return fmt.Errorf("invalid path %q", p)
	}
	if path.IsAbs(p) || path.Clean(p) != p || strings.HasPrefix(p, "../") || p == ".." {
		return fmt.Errorf("path %q must be clean and relative", p)
	}
	prefix, rest, ok := strings.Cut(p, "/")
	if !ok || rest == "" {
		return fmt.Errorf("path %q must be inside a managed directory", p)
	}
	if _, managed := Units[prefix]; !managed {
		return fmt.Errorf("path %q is outside the managed directories", p)
	}
	if !managedFile(p) {
		return fmt.Errorf("path %q is not a file the agent manages in %s/", p, prefix)
	}
	return nil
}

// hasFiles reports whether target holds any file of the unit's managed directory.
func hasFiles(unit string, target map[string][]byte) bool {
	for p := range target {
		if Units[strings.SplitN(p, "/", 2)[0]] == unit {
			return true
		}
	}
	return false
}

// activate makes a unit pick up its files, which are now those of target: it reloads the unit (or
// restarts it, for units that cannot reload). On-demand units (see onDemand) are the exception: they are
// started when their files appear and stopped when they are gone.
func (a *Applier) activate(ctx context.Context, unit string, target map[string][]byte) error {
	if onDemand[unit] {
		if !hasFiles(unit, target) {
			_, err := a.Runner.Run(ctx, "systemctl", "stop", unit)
			return err
		}
		if _, err := a.Runner.Run(ctx, "systemctl", "is-active", "--quiet", unit); err != nil {
			_, err = a.Runner.Run(ctx, "systemctl", "start", unit)
			return err
		}
	}
	_, err := a.Runner.Run(ctx, "systemctl", verb(unit), unit)
	return err
}

func relTo(root, p string) string {
	rel, _ := filepath.Rel(root, p)
	return rel
}

func (a *Applier) read(rel string) ([]byte, bool) {
	data, err := os.ReadFile(filepath.Join(a.Root, filepath.FromSlash(rel)))
	if err != nil {
		return nil, false
	}
	return data, true
}

// managed returns the files currently on disk under the managed directories.
func (a *Applier) managed() (map[string][]byte, error) {
	out := map[string][]byte{}
	for prefix := range Units {
		base := filepath.Join(a.Root, prefix)
		err := filepath.WalkDir(base, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				if errors.Is(err, fs.ErrNotExist) {
					return nil
				}
				return err
			}
			if d.Type().IsRegular() && managedFile(filepath.ToSlash(relTo(a.Root, p))) {
				rel, _ := filepath.Rel(a.Root, p)
				data, rerr := os.ReadFile(p)
				if rerr != nil {
					return rerr
				}
				out[filepath.ToSlash(rel)] = data
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (a *Applier) write(rel string, data []byte) error {
	target := filepath.Join(a.Root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(target), ".sy-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	mode := os.FileMode(0o640)
	if secretDirs[strings.SplitN(rel, "/", 2)[0]] {
		mode = 0o600
	}
	if err := os.Chmod(name, mode); err != nil {
		return err
	}
	if group, ok := groupDirs[strings.SplitN(rel, "/", 2)[0]]; ok {
		shareWithGroup(name, group)
		shareWithGroup(filepath.Dir(target), group)
	}
	return os.Rename(name, target)
}

// shareWithGroup makes path belong to group (best effort: the group exists only on engines whose image has the sidecar,
// and on systems without numeric groups the call is a no-op).
func shareWithGroup(path, group string) {
	g, err := user.LookupGroup(group)
	if err != nil {
		return
	}
	gid, err := strconv.Atoi(g.Gid)
	if err != nil {
		return
	}
	_ = os.Chown(path, -1, gid)
}

func (a *Applier) remove(rel string) error {
	err := os.Remove(filepath.Join(a.Root, filepath.FromSlash(rel)))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

// Apply verifies and applies a request. Failures are reported in the response (Ok=false).
func (a *Applier) Apply(ctx context.Context, req *pb.ApplyBundleRequest) *pb.ApplyBundleResponse {
	fail := func(format string, args ...any) *pb.ApplyBundleResponse {
		return &pb.ApplyBundleResponse{Ok: false, Error: fmt.Sprintf(format, args...)}
	}
	if len(a.PubKey) != ed25519.PublicKeySize || !ed25519.Verify(a.PubKey, req.Bundle, req.Signature) {
		return fail("signature verification failed")
	}
	var b pb.Bundle
	if err := proto.Unmarshal(req.Bundle, &b); err != nil {
		return fail("malformed bundle: %v", err)
	}
	if err := a.checkBinding(&b); err != nil {
		return fail("%v", err)
	}
	if len(b.Files) > maxFiles {
		return fail("too many files (%d)", len(b.Files))
	}
	want := map[string][]byte{}
	for _, f := range b.Files {
		if err := ValidatePath(f.Path); err != nil {
			return fail("%v", err)
		}
		if len(f.Content) > maxFileSize {
			return fail("file %s is too large", f.Path)
		}
		if _, dup := want[f.Path]; dup {
			return fail("duplicate path %s", f.Path)
		}
		want[f.Path] = f.Content
	}
	have, err := a.managed()
	if err != nil {
		return fail("reading current files: %v", err)
	}

	var changed []string
	for p, data := range want {
		if old, ok := have[p]; !ok || !bytes.Equal(old, data) {
			changed = append(changed, p)
		}
	}
	for p := range have {
		if _, keep := want[p]; !keep {
			changed = append(changed, p)
		}
	}
	sort.Strings(changed)

	units := map[string]bool{}
	for _, p := range changed {
		units[Units[strings.SplitN(p, "/", 2)[0]]] = true
	}
	var unitList []string
	for u := range units {
		unitList = append(unitList, u)
	}
	sort.Strings(unitList)

	// set makes the disk match target for every changed path; current says what is there now.
	set := func(target, current map[string][]byte) error {
		for _, p := range changed {
			if data, ok := target[p]; ok {
				if err := a.write(p, data); err != nil {
					return err
				}
			} else if _, ok := current[p]; ok {
				if err := a.remove(p); err != nil {
					return err
				}
			}
		}
		return nil
	}
	if err := set(want, have); err != nil {
		_ = set(have, want)
		return fail("writing files: %v", err)
	}

	resp := &pb.ApplyBundleResponse{Ok: true, ChangedFiles: changed}
	for _, unit := range unitList {
		if err := a.activate(ctx, unit, want); err != nil {
			// Put the previous files back and reload again so running daemons match the disk.
			_ = set(have, want)
			for _, u := range unitList {
				_ = a.activate(ctx, u, have)
			}
			return &pb.ApplyBundleResponse{
				Ok:               false,
				Error:            fmt.Sprintf("reloading %s failed: %v", unit, err),
				ChangedFiles:     changed,
				RestoredPrevious: true,
			}
		}
		resp.ReloadedUnits = append(resp.ReloadedUnits, unit)
	}
	if err := a.write("agent/bundle-id", []byte(b.Id)); err != nil {
		resp.Error = fmt.Sprintf("bundle applied but its id was not saved: %v", err)
	}
	if b.Sequence != 0 {
		if err := a.write(seqFile, []byte(strconv.FormatUint(b.Sequence, 10))); err != nil && resp.Error == "" {
			resp.Error = fmt.Sprintf("bundle applied but its sequence was not saved: %v", err)
		}
	}
	return resp
}

// LastBundleID returns the id of the last applied bundle, or "".
func (a *Applier) LastBundleID() string {
	data, ok := a.read("agent/bundle-id")
	if !ok {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// Hashes returns the SHA-256 of every managed file.
func (a *Applier) Hashes() (map[string]string, error) {
	files, err := a.managed()
	if err != nil {
		return nil, err
	}
	out := make(map[string]string, len(files))
	for p, data := range files {
		out[p] = Hash(data)
	}
	return out, nil
}
