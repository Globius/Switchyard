// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// Client talks to the controller API as one tenant.
//
// Writes are serialised: every write is a change set that the controller applies behind a
// per-SE-group lock, so concurrent writes only queue up and may time out on that lock.
type Client struct {
	base    string
	token   string
	tenant  string
	http    *http.Client
	timeout time.Duration
	poll    time.Duration
	writeMu sync.Mutex
}

// ClientConfig is what NewClient needs.
type ClientConfig struct {
	Endpoint string
	Token    string
	Tenant   string
	// CAFile is an extra CA certificate (PEM) to trust.
	CAFile string
	// Insecure skips certificate verification. Labs only.
	Insecure bool
	// ApplyTimeout bounds the wait for one change set to apply.
	ApplyTimeout time.Duration
	// PollInterval is the delay between job polls.
	PollInterval time.Duration
}

// NewClient validates the configuration and builds a client.
func NewClient(cfg ClientConfig) (*Client, error) {
	u, err := url.Parse(cfg.Endpoint)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return nil, fmt.Errorf("endpoint %q must be an http(s) URL such as https://switchyard.example.test", cfg.Endpoint)
	}
	if u.User != nil {
		return nil, fmt.Errorf("endpoint must not carry credentials; use the token argument")
	}
	if cfg.Token == "" || cfg.Tenant == "" {
		return nil, fmt.Errorf("token and tenant are required")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: cfg.Insecure} //nolint:gosec // opt-in, documented
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("reading ca_file: %w", err)
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("ca_file %s holds no certificate", cfg.CAFile)
		}
		tlsConfig.RootCAs = pool
	}
	transport.TLSClientConfig = tlsConfig
	c := &Client{
		base:    strings.TrimRight(cfg.Endpoint, "/") + "/api/v1",
		token:   cfg.Token,
		tenant:  url.PathEscape(cfg.Tenant),
		http:    &http.Client{Transport: transport, Timeout: 60 * time.Second},
		timeout: cfg.ApplyTimeout,
		poll:    cfg.PollInterval,
	}
	if c.timeout <= 0 {
		c.timeout = 10 * time.Minute
	}
	if c.poll <= 0 {
		c.poll = time.Second
	}
	return c, nil
}

// APIError is a non-2xx answer from the controller.
type APIError struct {
	Status int
	Title  string
	Detail string
	Errors []string
}

func (e *APIError) Error() string {
	msg := fmt.Sprintf("controller answered HTTP %d", e.Status)
	if e.Title != "" {
		msg += ": " + e.Title
	}
	if e.Detail != "" {
		msg += ": " + e.Detail
	}
	for _, line := range e.Errors {
		msg += "\n  - " + line
	}
	return msg
}

type response struct {
	status int
	etag   string
	body   []byte
}

func (c *Client) do(ctx context.Context, method, path string, query url.Values, header map[string]string, body any) (*response, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(raw)
	}
	target := c.base + path
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "terraform-provider-switchyard")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	res, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 16<<20))
	if err != nil {
		return nil, err
	}
	out := &response{status: res.StatusCode, etag: res.Header.Get("ETag"), body: raw}
	if res.StatusCode >= 300 {
		return out, parseProblem(res.StatusCode, raw)
	}
	return out, nil
}

// parseProblem reads an RFC 9457 problem document; anything else becomes the status line.
func parseProblem(status int, raw []byte) *APIError {
	e := &APIError{Status: status}
	var p struct {
		Title  string `json:"title"`
		Detail string `json:"detail"`
		Errors []struct {
			Path    string `json:"path"`
			Message string `json:"message"`
		} `json:"errors"`
	}
	if json.Unmarshal(raw, &p) == nil {
		e.Title, e.Detail = p.Title, p.Detail
		for _, f := range p.Errors {
			e.Errors = append(e.Errors, f.Path+": "+f.Message)
		}
	}
	return e
}

func (c *Client) collection(slug string) string {
	return "/tenants/" + c.tenant + "/" + slug
}

// Get returns one object, or nil when it does not exist.
func (c *Client) Get(ctx context.Context, slug, id string) (map[string]any, error) {
	res, err := c.do(ctx, http.MethodGet, c.collection(slug)+"/"+url.PathEscape(id), nil, nil, nil)
	if err != nil {
		if ae, ok := err.(*APIError); ok && ae.Status == http.StatusNotFound {
			return nil, nil
		}
		return nil, err
	}
	return decodeObject(res.body)
}

// FindByName returns the object called name, or nil. It reads every page of the collection.
func (c *Client) FindByName(ctx context.Context, slug, name string) (map[string]any, error) {
	cursor := ""
	for {
		q := url.Values{"limit": {"200"}}
		if cursor != "" {
			q.Set("cursor", cursor)
		}
		res, err := c.do(ctx, http.MethodGet, c.collection(slug), q, nil, nil)
		if err != nil {
			return nil, err
		}
		var page struct {
			Items      []map[string]any `json:"items"`
			NextCursor *string          `json:"next_cursor"`
		}
		if err := json.Unmarshal(res.body, &page); err != nil {
			return nil, fmt.Errorf("reading %s list: %w", slug, err)
		}
		for _, item := range page.Items {
			if item["name"] == name {
				return item, nil
			}
		}
		if page.NextCursor == nil || *page.NextCursor == "" {
			return nil, nil
		}
		cursor = *page.NextCursor
	}
}

func decodeObject(raw []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var obj map[string]any
	if err := dec.Decode(&obj); err != nil {
		return nil, fmt.Errorf("reading the controller's answer: %w", err)
	}
	return obj, nil
}

// accepted is the 202 body of a write.
type accepted struct {
	ChangeSetID string  `json:"change_set_id"`
	ObjectID    *string `json:"object_id"`
	JobID       *string `json:"job_id"`
}

// write sends one object write with apply=true and waits for the job that applies it.
func (c *Client) write(ctx context.Context, method, slug, id string, objectID string, header map[string]string, body any) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	path := c.collection(slug)
	q := url.Values{"apply": {"true"}}
	if id != "" {
		path += "/" + url.PathEscape(id)
	}
	if objectID != "" {
		q.Set("object_id", objectID)
	}
	res, err := c.do(ctx, method, path, q, header, body)
	if err != nil {
		return err
	}
	var acc accepted
	if err := json.Unmarshal(res.body, &acc); err != nil {
		return fmt.Errorf("reading the controller's answer: %w", err)
	}
	if acc.JobID == nil {
		return fmt.Errorf("the controller queued change set %s but started no job", acc.ChangeSetID)
	}
	return c.waitForJob(ctx, *acc.JobID)
}

// Create creates the object with the chosen id.
func (c *Client) Create(ctx context.Context, slug, id string, body map[string]any) error {
	return c.write(ctx, http.MethodPost, slug, "", id, nil, body)
}

// Update patches the object; version is the one the caller last read.
func (c *Client) Update(ctx context.Context, slug, id string, version int64, body map[string]any) error {
	return c.write(ctx, http.MethodPatch, slug, id, "", map[string]string{"If-Match": fmt.Sprintf(`"%d"`, version)}, body)
}

// Delete removes the object. A missing object is not an error.
func (c *Client) Delete(ctx context.Context, slug, id string, version int64) error {
	err := c.write(ctx, http.MethodDelete, slug, id, "", map[string]string{"If-Match": fmt.Sprintf(`"%d"`, version)}, nil)
	if ae, ok := err.(*APIError); ok && ae.Status == http.StatusNotFound {
		return nil
	}
	return err
}

// waitForJob polls until the apply job finishes. A job that finished but reports ok=false was
// rejected or reverted (for example by the canary) and is an error.
func (c *Client) waitForJob(ctx context.Context, jobID string) error {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	for {
		res, err := c.do(ctx, http.MethodGet, "/tenants/"+c.tenant+"/jobs/"+url.PathEscape(jobID), nil, nil, nil)
		if err != nil {
			return err
		}
		var job struct {
			State string  `json:"state"`
			OK    *bool   `json:"ok"`
			Error *string `json:"error"`
			Retry bool    `json:"retrying"`
		}
		if err := json.Unmarshal(res.body, &job); err != nil {
			return fmt.Errorf("reading job %s: %w", jobID, err)
		}
		switch job.State {
		case "succeeded":
			if job.OK != nil && !*job.OK {
				return fmt.Errorf("the controller rejected or reverted the change%s", jobError(job.Error, res.body))
			}
			return nil
		case "failed", "dead", "cancelled":
			return fmt.Errorf("job %s %s%s", jobID, job.State, jobError(job.Error, res.body))
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("job %s did not finish within %s", jobID, c.timeout)
		case <-time.After(c.poll):
		}
	}
}

func jobError(top *string, raw []byte) string {
	if top != nil && *top != "" {
		return ": " + *top
	}
	var j struct {
		Result struct {
			Error string `json:"error"`
		} `json:"result"`
	}
	if json.Unmarshal(raw, &j) == nil && j.Result.Error != "" {
		return ": " + j.Result.Error
	}
	return ""
}
