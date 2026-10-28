// Package engine is a minimal client of the Docker Engine API (which Podman's API service also serves,
// local-runner.md L-3), enough for runner-local's executor: images, volumes, containers, exec, archive.
package engine

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client talks to unix:///path or tcp://host:port.
type Client struct {
	hc   *http.Client
	base string
	ver  string
}

// New creates a client for host (RUNNER_CONTAINER_HOST).
func New(host string) (*Client, error) {
	c := &Client{ver: "v1.41"}
	switch {
	case strings.HasPrefix(host, "unix://"):
		path := strings.TrimPrefix(host, "unix://")
		tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", path)
		}}
		c.hc = &http.Client{Transport: tr}
		c.base = "http://engine"
	case strings.HasPrefix(host, "tcp://"):
		c.hc = &http.Client{}
		c.base = "http://" + strings.TrimPrefix(host, "tcp://")
	case strings.HasPrefix(host, "http://"):
		c.hc = &http.Client{}
		c.base = host
	default:
		return nil, fmt.Errorf("unsupported container host %q", host)
	}
	return c, nil
}

// APIError is a non-2xx answer.
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return fmt.Sprintf("engine HTTP %d: %s", e.Status, e.Message) }

// IsNotFound reports a 404.
func IsNotFound(err error) bool {
	var ae *APIError
	return errors.As(err, &ae) && ae.Status == 404
}

// IsConflict reports a 409.
func IsConflict(err error) bool {
	var ae *APIError
	return errors.As(err, &ae) && ae.Status == 409
}

func (c *Client) do(ctx context.Context, method, path string, q url.Values, body any, out any) error {
	resp, err := c.raw(ctx, method, path, q, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}

func (c *Client) raw(ctx context.Context, method, path string, q url.Values, body any, ctype string) (*http.Response, error) {
	var rd io.Reader
	if body != nil {
		switch b := body.(type) {
		case io.Reader:
			rd = b
		default:
			jb, err := json.Marshal(body)
			if err != nil {
				return nil, err
			}
			rd = bytes.NewReader(jb)
			ctype = "application/json"
		}
	}
	u := c.base + "/" + c.ver + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, rd)
	if err != nil {
		return nil, err
	}
	if ctype != "" {
		req.Header.Set("Content-Type", ctype)
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 && resp.StatusCode != 304 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		var m struct {
			Message string `json:"message"`
			Cause   string `json:"cause"`
		}
		_ = json.Unmarshal(b, &m)
		msg := m.Message
		if msg == "" {
			msg = strings.TrimSpace(string(b))
		}
		return nil, &APIError{Status: resp.StatusCode, Message: msg}
	}
	return resp, nil
}

// Version is GET /version.
type Version struct {
	Version    string `json:"Version"`
	APIVersion string `json:"ApiVersion"`
	Os         string `json:"Os"`
	Arch       string `json:"Arch"`
	Components []struct {
		Name string `json:"Name"`
	} `json:"Components"`
}

func (c *Client) Version(ctx context.Context) (*Version, error) {
	var v Version
	resp, err := c.hc.Get(c.base + "/version")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, &APIError{Status: resp.StatusCode}
	}
	return &v, json.NewDecoder(resp.Body).Decode(&v)
}

// Engine returns "podman" or "docker".
func (v *Version) Engine() string {
	for _, c := range v.Components {
		if strings.Contains(strings.ToLower(c.Name), "podman") {
			return "podman"
		}
	}
	return "docker"
}

// ImageExists inspects an image.
func (c *Client) ImageExists(ctx context.Context, ref string) (bool, error) {
	err := c.do(ctx, http.MethodGet, "/images/"+url.PathEscape(ref)+"/json", nil, nil, nil)
	if IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

// Pull pulls ref and returns an error message from the stream if any.
func (c *Client) Pull(ctx context.Context, ref string) error {
	name, tag := ref, "latest"
	if i := strings.LastIndex(ref, "@"); i >= 0 {
		name, tag = ref[:i], ref[i+1:]
	} else if i := strings.LastIndex(ref, ":"); i > strings.LastIndex(ref, "/") {
		name, tag = ref[:i], ref[i+1:]
	}
	q := url.Values{"fromImage": {name}, "tag": {tag}}
	resp, err := c.raw(ctx, http.MethodPost, "/images/create", q, nil, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	dec := json.NewDecoder(resp.Body)
	var lastErr string
	for {
		var m struct {
			Error string `json:"error"`
		}
		if err := dec.Decode(&m); err != nil {
			break
		}
		if m.Error != "" {
			lastErr = m.Error
		}
	}
	if lastErr != "" {
		return errors.New(lastErr)
	}
	return nil
}

// VolumeCreate creates a named volume (idempotent). driverOpts may carry type/device/o for tmpfs volumes.
func (c *Client) VolumeCreate(ctx context.Context, name string, labels, driverOpts map[string]string) error {
	body := map[string]any{"Name": name, "Labels": labels}
	if len(driverOpts) > 0 {
		body["Driver"] = "local"
		body["DriverOpts"] = driverOpts
	}
	err := c.do(ctx, http.MethodPost, "/volumes/create", nil, body, nil)
	if IsConflict(err) {
		return nil
	}
	return err
}

// VolumeRemove removes a volume.
func (c *Client) VolumeRemove(ctx context.Context, name string) error {
	err := c.do(ctx, http.MethodDelete, "/volumes/"+url.PathEscape(name), url.Values{"force": {"true"}}, nil, nil)
	if IsNotFound(err) {
		return nil
	}
	return err
}

// Volumes lists volumes with a label filter.
func (c *Client) Volumes(ctx context.Context, label string) ([]string, error) {
	f, _ := json.Marshal(map[string][]string{"label": {label}})
	var out struct {
		Volumes []struct {
			Name string `json:"Name"`
		} `json:"Volumes"`
	}
	if err := c.do(ctx, http.MethodGet, "/volumes", url.Values{"filters": {string(f)}}, nil, &out); err != nil {
		return nil, err
	}
	var names []string
	for _, v := range out.Volumes {
		names = append(names, v.Name)
	}
	return names, nil
}

// Mount is a HostConfig.Mounts entry.
type Mount struct {
	Type          string         `json:"Type"` // volume | bind | tmpfs
	Source        string         `json:"Source,omitempty"`
	Target        string         `json:"Target"`
	ReadOnly      bool           `json:"ReadOnly,omitempty"`
	VolumeOptions *VolumeOptions `json:"VolumeOptions,omitempty"`
	TmpfsOptions  *TmpfsOptions  `json:"TmpfsOptions,omitempty"`
}

type VolumeOptions struct {
	NoCopy  bool   `json:"NoCopy,omitempty"`
	Subpath string `json:"Subpath,omitempty"`
}

type TmpfsOptions struct {
	SizeBytes int64 `json:"SizeBytes,omitempty"`
	Mode      int   `json:"Mode,omitempty"`
}

// HostConfig subset.
type HostConfig struct {
	NetworkMode    string            `json:"NetworkMode,omitempty"`
	Mounts         []Mount           `json:"Mounts,omitempty"`
	ReadonlyRootfs bool              `json:"ReadonlyRootfs,omitempty"`
	CapDrop        []string          `json:"CapDrop,omitempty"`
	CapAdd         []string          `json:"CapAdd,omitempty"`
	SecurityOpt    []string          `json:"SecurityOpt,omitempty"`
	PidsLimit      int64             `json:"PidsLimit,omitempty"`
	Memory         int64             `json:"Memory,omitempty"`
	NanoCpus       int64             `json:"NanoCpus,omitempty"`
	GroupAdd       []string          `json:"GroupAdd,omitempty"`
	DNS            []string          `json:"Dns,omitempty"`
	Runtime        string            `json:"Runtime,omitempty"`
	Init           *bool             `json:"Init,omitempty"`
	Tmpfs          map[string]string `json:"Tmpfs,omitempty"`
	ExtraHosts     []string          `json:"ExtraHosts,omitempty"`
	RestartPolicy  *RestartPolicy    `json:"RestartPolicy,omitempty"`
}

// RestartPolicy of a container.
type RestartPolicy struct {
	Name              string `json:"Name"`
	MaximumRetryCount int    `json:"MaximumRetryCount,omitempty"`
}

// ContainerConfig is the create body.
type ContainerConfig struct {
	Image            string            `json:"Image"`
	Cmd              []string          `json:"Cmd,omitempty"`
	Entrypoint       []string          `json:"Entrypoint,omitempty"`
	Env              []string          `json:"Env,omitempty"`
	User             string            `json:"User,omitempty"`
	WorkingDir       string            `json:"WorkingDir,omitempty"`
	Labels           map[string]string `json:"Labels,omitempty"`
	StopTimeout      *int              `json:"StopTimeout,omitempty"`
	StopSignal       string            `json:"StopSignal,omitempty"`
	Hostname         string            `json:"Hostname,omitempty"`
	HostConfig       HostConfig        `json:"HostConfig"`
	NetworkingConfig *NetworkingConfig `json:"NetworkingConfig,omitempty"`
}

type NetworkingConfig struct {
	EndpointsConfig map[string]EndpointSettings `json:"EndpointsConfig"`
}

type EndpointSettings struct {
	Aliases []string `json:"Aliases,omitempty"`
}

// Create creates a container and returns its ID.
func (c *Client) Create(ctx context.Context, name string, cfg ContainerConfig) (string, error) {
	var out struct {
		ID string `json:"Id"`
	}
	err := c.do(ctx, http.MethodPost, "/containers/create", url.Values{"name": {name}}, cfg, &out)
	return out.ID, err
}

// Start starts a container (idempotent: 304 is success).
func (c *Client) Start(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/start", nil, nil, nil)
}

// Stop stops with a timeout in seconds.
func (c *Client) Stop(ctx context.Context, id string, seconds int) error {
	err := c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/stop", url.Values{"t": {fmt.Sprint(seconds)}}, nil, nil)
	if IsNotFound(err) {
		return nil
	}
	return err
}

// Pause freezes a container; Unpause thaws it (test drivers: RC-LEASE-01).
func (c *Client) Pause(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/pause", nil, nil, nil)
}

func (c *Client) Unpause(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/unpause", nil, nil, nil)
}

// Kill sends a signal.
func (c *Client) Kill(ctx context.Context, id, signal string) error {
	err := c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/kill", url.Values{"signal": {signal}}, nil, nil)
	if IsNotFound(err) || IsConflict(err) {
		return nil
	}
	return err
}

// Remove force-removes a container together with its anonymous volumes (v=true: image VOLUME directives and
// unnamed mounts). Named volumes are never removed by the Docker or Podman API here; the executor removes the
// per-run named volumes itself (gcContainers).
func (c *Client) Remove(ctx context.Context, id string) error {
	err := c.do(ctx, http.MethodDelete, "/containers/"+url.PathEscape(id), url.Values{"force": {"true"}, "v": {"true"}}, nil, nil)
	if IsNotFound(err) {
		return nil
	}
	return err
}

// State is the inspect State subset.
type State struct {
	Status     string `json:"Status"` // created running exited ...
	Running    bool   `json:"Running"`
	OOMKilled  bool   `json:"OOMKilled"`
	ExitCode   int    `json:"ExitCode"`
	Error      string `json:"Error"`
	StartedAt  string `json:"StartedAt"`
	FinishedAt string `json:"FinishedAt"`
}

// Inspect is the inspect subset.
type Inspect struct {
	ID     string `json:"Id"`
	Name   string `json:"Name"`
	State  State  `json:"State"`
	Config struct {
		Labels map[string]string `json:"Labels"`
		Image  string            `json:"Image"`
	} `json:"Config"`
	Mounts []struct {
		Type        string `json:"Type"`
		Name        string `json:"Name"`
		Source      string `json:"Source"`
		Destination string `json:"Destination"`
	} `json:"Mounts"`
}

// StartedTime parses StartedAt.
func (s State) StartedTime() time.Time  { return parseTime(s.StartedAt) }
func (s State) FinishedTime() time.Time { return parseTime(s.FinishedAt) }

func parseTime(v string) time.Time {
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil || t.Year() < 2000 {
		return time.Time{}
	}
	return t
}

// InspectContainer returns nil, nil when the container does not exist.
func (c *Client) InspectContainer(ctx context.Context, id string) (*Inspect, error) {
	var out Inspect
	err := c.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/json", nil, nil, &out)
	if IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// Summary is a list entry.
type Summary struct {
	ID     string            `json:"Id"`
	Names  []string          `json:"Names"`
	Labels map[string]string `json:"Labels"`
	State  string            `json:"State"`
}

// List lists containers (all) matching a label filter like "k=v".
func (c *Client) List(ctx context.Context, label string) ([]Summary, error) {
	f, _ := json.Marshal(map[string][]string{"label": {label}})
	var out []Summary
	err := c.do(ctx, http.MethodGet, "/containers/json", url.Values{"all": {"true"}, "filters": {string(f)}}, nil, &out)
	return out, err
}

// Exec runs cmd in a running container and returns its exit code.
func (c *Client) Exec(ctx context.Context, id string, cmd []string, user string) (int, error) {
	var created struct {
		ID string `json:"Id"`
	}
	body := map[string]any{"Cmd": cmd, "AttachStdout": true, "AttachStderr": true} // podman compat refuses detached-less exec without attach (500)
	if user != "" {
		body["User"] = user
	}
	if err := c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/exec", nil, body, &created); err != nil {
		return -1, err
	}
	resp, err := c.raw(ctx, http.MethodPost, "/exec/"+created.ID+"/start", nil, map[string]any{"Detach": false, "Tty": false}, "")
	if err != nil {
		return -1, err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	for i := 0; i < 50; i++ {
		var ins struct {
			Running  bool `json:"Running"`
			ExitCode int  `json:"ExitCode"`
		}
		if err := c.do(ctx, http.MethodGet, "/exec/"+created.ID+"/json", nil, nil, &ins); err != nil {
			return -1, err
		}
		if !ins.Running {
			return ins.ExitCode, nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return -1, errors.New("exec did not finish")
}

// ReadFile reads one regular file out of a container (running or stopped) via the archive endpoint.
func (c *Client) ReadFile(ctx context.Context, id, path string, max int64) ([]byte, error) {
	resp, err := c.raw(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/archive", url.Values{"path": {path}}, nil, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	tr := tar.NewReader(resp.Body)
	for {
		h, err := tr.Next()
		if err != nil {
			return nil, err
		}
		if h.Typeflag == tar.TypeReg {
			return io.ReadAll(io.LimitReader(tr, max))
		}
	}
}

// Logs returns the last lines of a container's combined log (demultiplexed best-effort).
func (c *Client) Logs(ctx context.Context, id string, tail int) (string, error) {
	resp, err := c.raw(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/logs",
		url.Values{"stdout": {"true"}, "stderr": {"true"}, "tail": {fmt.Sprint(tail)}}, nil, "")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	// strip the 8-byte stream headers when present
	var out bytes.Buffer
	for len(b) >= 8 && (b[0] == 1 || b[0] == 2) && b[1] == 0 && b[2] == 0 && b[3] == 0 {
		n := int(b[4])<<24 | int(b[5])<<16 | int(b[6])<<8 | int(b[7])
		if 8+n > len(b) {
			break
		}
		out.Write(b[8 : 8+n])
		b = b[8+n:]
	}
	out.Write(b)
	return out.String(), nil
}

// LogLine is one log line with the engine's timestamp.
type LogLine struct {
	At   time.Time
	Line string
}

// LogsAll returns every stdout+stderr line of a (running or stopped) container with its timestamp, oldest first.
func (c *Client) LogsAll(ctx context.Context, id string, max int64) ([]LogLine, error) {
	resp, err := c.raw(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/logs",
		url.Values{"stdout": {"true"}, "stderr": {"true"}, "timestamps": {"true"}}, nil, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, max))
	var raw bytes.Buffer
	for len(b) >= 8 && (b[0] == 1 || b[0] == 2) && b[1] == 0 && b[2] == 0 && b[3] == 0 {
		n := int(b[4])<<24 | int(b[5])<<16 | int(b[6])<<8 | int(b[7])
		if 8+n > len(b) {
			break
		}
		raw.Write(b[8 : 8+n])
		b = b[8+n:]
	}
	raw.Write(b)
	var out []LogLine
	for _, l := range strings.Split(raw.String(), "\n") {
		ts, line, ok := strings.Cut(l, " ")
		if !ok {
			continue
		}
		t, err := time.Parse(time.RFC3339Nano, ts)
		if err != nil {
			continue
		}
		out = append(out, LogLine{At: t, Line: strings.TrimRight(line, "\r")})
	}
	return out, nil
}

// SELinuxEnabled reports whether the engine host enforces SELinux labels (GET /info SecurityOptions).
func (c *Client) SELinuxEnabled(ctx context.Context) bool {
	var info struct {
		SecurityOptions []string `json:"SecurityOptions"`
	}
	if err := c.do(ctx, http.MethodGet, "/info", nil, nil, &info); err != nil {
		return false
	}
	for _, o := range info.SecurityOptions {
		if strings.Contains(o, "selinux") {
			return true
		}
	}
	return false
}
