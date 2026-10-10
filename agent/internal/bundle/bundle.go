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
	"path"
	"path/filepath"
	"sort"
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
	"sysctl":     "systemd-sysctl",
}

// verbs overrides the systemctl verb for units that cannot reload (default "reload"). The
// sysctl directory is applied by restarting systemd-sysctl, which re-reads the settings.
var verbs = map[string]string{
	"systemd-sysctl": "restart",
}

func verb(unit string) string {
	if v, ok := verbs[unit]; ok {
		return v
	}
	return "reload"
}

// suffixOnly restricts a managed directory to files with one suffix. The Data Plane API keeps its
// crt-lists in the same directory as the certificates, so only "*.pem" files there are the
// agent's; everything else is left alone and never removed.
var suffixOnly = map[string]string{"certs": ".pem"}

// secretDirs hold private keys and are written with owner-only permissions.
var secretDirs = map[string]bool{"certs": true}

func managedFile(rel string) bool {
	prefix, rest, ok := strings.Cut(rel, "/")
	if !ok {
		return false
	}
	suffix, restricted := suffixOnly[prefix]
	return !restricted || (strings.HasSuffix(rest, suffix) && !strings.Contains(rest, "/"))
}

// Applier applies bundles under Root.
type Applier struct {
	Root   string
	PubKey ed25519.PublicKey
	Runner runner.Runner
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
// restarts it, for units that cannot reload). Keepalived is the exception because it only runs
// when it has something to do: it is started when its files appear and stopped when they are gone.
func (a *Applier) activate(ctx context.Context, unit string, target map[string][]byte) error {
	if unit == "keepalived" {
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
	return os.Rename(name, target)
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
