// Package protocol is the device agent's side of the posture API (ADR 0196): the facts it reports,
// the signature that authenticates a report, and the two calls (enroll, report).
//
// A report is authenticated by an Ed25519 signature over
//
//	METHOD "\n" PATH "\n" TIMESTAMP_MS "\n" HEX(SHA-256(body))
//
// where PATH is the request path the controller sees. The controller refuses a timestamp that is not
// newer than the last one it accepted for the device or that is more than five minutes off, so a
// captured request cannot be replayed. The signature vector in testdata/vector.json is checked by
// this package and by the controller's tests, so the two sides cannot drift apart.
package protocol

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Paths of the controller's device endpoints, relative to its base URL.
const (
	EnrollPath = "/api/v1/device/enroll"
	ReportPath = "/api/v1/device/report"
	// AgentVersion is reported in every report.
	AgentVersion = "1.0.0"
)

// Facts is what the agent measured. Pointer fields are null when the agent cannot tell, and the
// controller then fails the requirement that needs them.
type Facts struct {
	OS              string  `json:"os"`
	OSVersion       string  `json:"os_version"`
	DiskEncrypted   bool    `json:"disk_encrypted"`
	FirewallEnabled bool    `json:"firewall_enabled"`
	ScreenLock      *bool   `json:"screen_lock"`
	DaysSinceUpdate *int    `json:"days_since_update"`
	AgentVersion    string  `json:"agent_version"`
	Hostname        *string `json:"hostname,omitempty"`
}

// Report is the body of a posture report.
type Report struct {
	Facts Facts  `json:"facts"`
	CSR   string `json:"csr,omitempty"`
}

// ReportResult is the controller's verdict.
type ReportResult struct {
	Compliant   bool     `json:"compliant"`
	Failures    []string `json:"failures"`
	Certificate string   `json:"certificate"`
	CA          string   `json:"ca"`
	NotAfter    string   `json:"not_after"`
	NextReportS int      `json:"next_report_s"`
}

// EnrollResult is the identity the controller gives a device.
type EnrollResult struct {
	DeviceID string `json:"device_id"`
	Tenant   string `json:"tenant"`
	Name     string `json:"name"`
	Policy   string `json:"policy"`
	CertTTLH int    `json:"cert_ttl_h"`
}

// Canonical returns the bytes that are signed.
func Canonical(method, path string, tsMillis int64, body []byte) []byte {
	sum := sha256.Sum256(body)
	return []byte(strings.ToUpper(method) + "\n" + path + "\n" + strconv.FormatInt(tsMillis, 10) + "\n" + hex.EncodeToString(sum[:]))
}

// Sign returns the base64 signature of a request.
func Sign(key ed25519.PrivateKey, method, path string, tsMillis int64, body []byte) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(key, Canonical(method, path, tsMillis, body)))
}

// Client talks to one controller.
type Client struct {
	Base *url.URL
	HTTP *http.Client
	// Now is the clock; nil means time.Now.
	Now func() time.Time
}

// NewClient parses the controller URL. Only https is accepted unless allowPlainHTTP is set (tests):
// the enrollment response and every report carry material an eavesdropper could use.
func NewClient(raw string, httpClient *http.Client, allowPlainHTTP bool) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(raw, "/"))
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("the controller URL %q is not usable", raw)
	}
	if u.Scheme != "https" && !(allowPlainHTTP && u.Scheme == "http") {
		return nil, errors.New("the controller URL must use https")
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}
	return &Client{Base: u, HTTP: httpClient}, nil
}

func (c *Client) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

// pathOf is the path of an endpoint as the controller will see it (a base path is kept).
func (c *Client) pathOf(endpoint string) string {
	return strings.TrimRight(c.Base.Path, "/") + endpoint
}

func (c *Client) do(ctx context.Context, path string, body []byte, headers map[string]string, out any) error {
	target := *c.Base
	target.Path = path
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode/100 != 2 {
		var problem struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &problem)
		if problem.Detail == "" {
			problem.Detail = strings.TrimSpace(string(raw))
			if len(problem.Detail) > 200 {
				problem.Detail = problem.Detail[:200]
			}
		}
		return fmt.Errorf("the controller answered %d: %s", resp.StatusCode, problem.Detail)
	}
	return json.Unmarshal(raw, out)
}

// Enroll trades the one-time token and the Ed25519 public key for a device identity.
func (c *Client) Enroll(ctx context.Context, token, name string, pub ed25519.PublicKey, owner string) (EnrollResult, error) {
	body, err := json.Marshal(map[string]string{
		"token": token, "name": name, "owner": owner, "public_key": base64.StdEncoding.EncodeToString(pub),
	})
	if err != nil {
		return EnrollResult{}, err
	}
	var out EnrollResult
	err = c.do(ctx, c.pathOf(EnrollPath), body, nil, &out)
	return out, err
}

// SendReport signs and sends a report. The timestamp is the clock's, in milliseconds, and never
// below minTS+1, so reports from a device always rise even when the clock steps back a little.
func (c *Client) SendReport(ctx context.Context, deviceID string, key ed25519.PrivateKey, r Report, minTS int64) (ReportResult, int64, error) {
	body, err := json.Marshal(r)
	if err != nil {
		return ReportResult{}, 0, err
	}
	ts := c.now().UnixMilli()
	if ts <= minTS {
		ts = minTS + 1
	}
	path := c.pathOf(ReportPath)
	var out ReportResult
	err = c.do(ctx, path, body, map[string]string{
		"X-Device-Id":        deviceID,
		"X-Device-Timestamp": strconv.FormatInt(ts, 10),
		"X-Device-Signature": Sign(key, http.MethodPost, path, ts, body),
	}, &out)
	return out, ts, err
}
