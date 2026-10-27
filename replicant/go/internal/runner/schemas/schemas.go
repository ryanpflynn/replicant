// Package schemas embeds verbatim copies of the contract schemas (spec/00-contracts, spec/03 and spec/05
// schemas) and validates documents against them with JSON Schema 2020-12.
package schemas

import (
	"bytes"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"strings"
	"sync"

	"github.com/dlclark/regexp2"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"golang.org/x/text/language"
	"golang.org/x/text/message"
	"sigs.k8s.io/yaml"
)

//go:embed files/*
var files embed.FS

// Well-known schema locations.
const (
	RunnerAPI       = "https://schemas.replicant.example.com/contracts/runner-api/v2.json"
	RunSpec         = RunnerAPI + "#/components/schemas/RunSpec"
	RunStatus       = RunnerAPI + "#/components/schemas/RunStatus"
	RunResult       = "https://schemas.replicant.example.com/contracts/run-result/v1.json"
	RunManifest     = "https://schemas.replicant.example.com/contracts/run-manifest/v1.json"
	RunInputs       = "https://schemas.replicant.example.com/runtime/run-inputs/v1.json"
	RunOutputs      = "https://schemas.replicant.example.com/runtime/run-outputs/v1.json"
	RunPlan         = "https://schemas.replicant.example.com/runtime/run-plan/v1.json"
	RoleImplementer = "https://schemas.replicant.example.com/role-results/implementer/v1.json"
	RoleReviewer    = "https://schemas.replicant.example.com/role-results/reviewer/v1.json"
	RolePlanner     = "https://schemas.replicant.example.com/role-results/planner/v1.json"
	RoleTakeover    = "https://schemas.replicant.example.com/role-results/takeover/v1.json"
	RoleQA          = "https://schemas.replicant.example.com/qa/role-result-qa/v1.json"
)

// RoleSchemaFor maps a role_result schema id (replicant.role-result.<x>/v1) to its URL.
func RoleSchemaFor(id string) string {
	switch id {
	case "replicant.role-result.implementer/v1":
		return RoleImplementer
	case "replicant.role-result.reviewer/v1":
		return RoleReviewer
	case "replicant.role-result.planner/v1":
		return RolePlanner
	case "replicant.role-result.takeover/v1":
		return RoleTakeover
	case "replicant.role-result.qa/v1":
		return RoleQA
	}
	return ""
}

type regexp2Engine struct{ re *regexp2.Regexp }

func (r regexp2Engine) MatchString(s string) bool {
	ok, _ := r.re.MatchString(s)
	return ok
}
func (r regexp2Engine) String() string { return r.re.String() }

var (
	once     sync.Once
	compiler *jsonschema.Compiler
	docs     map[string]any
	initErr  error
	cacheMu  sync.Mutex
	cache    = map[string]*jsonschema.Schema{}
)

func load() {
	compiler = jsonschema.NewCompiler()
	compiler.UseRegexpEngine(func(s string) (jsonschema.Regexp, error) {
		re, err := regexp2.Compile(s, regexp2.ECMAScript)
		if err != nil {
			return nil, err
		}
		return regexp2Engine{re}, nil
	})
	docs = map[string]any{}
	entries, err := fs.ReadDir(files, "files")
	if err != nil {
		initErr = err
		return
	}
	for _, e := range entries {
		b, err := files.ReadFile("files/" + e.Name())
		if err != nil {
			initErr = err
			return
		}
		url := ""
		if strings.HasSuffix(e.Name(), ".yaml") {
			jb, err := yaml.YAMLToJSON(b)
			if err != nil {
				initErr = fmt.Errorf("%s: %w", e.Name(), err)
				return
			}
			b = jb
			url = RunnerAPI
		}
		doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(b))
		if err != nil {
			initErr = fmt.Errorf("%s: %w", e.Name(), err)
			return
		}
		if url == "" {
			m, _ := doc.(map[string]any)
			url, _ = m["$id"].(string)
		}
		if url == "" {
			initErr = fmt.Errorf("%s: no $id", e.Name())
			return
		}
		docs[url] = doc
		if err := compiler.AddResource(url, doc); err != nil {
			initErr = fmt.Errorf("%s: %w", e.Name(), err)
			return
		}
	}
}

// Compile returns the compiled schema at loc.
func Compile(loc string) (*jsonschema.Schema, error) {
	once.Do(load)
	if initErr != nil {
		return nil, initErr
	}
	cacheMu.Lock()
	defer cacheMu.Unlock()
	if s, ok := cache[loc]; ok {
		return s, nil
	}
	s, err := compiler.Compile(loc)
	if err != nil {
		return nil, err
	}
	cache[loc] = s
	return s, nil
}

// Violation is one failed rule.
type Violation struct {
	Field   string `json:"field"`
	Rule    string `json:"rule"`
	Message string `json:"message,omitempty"`
}

// Error carries violations.
type Error struct{ Violations []Violation }

func (e *Error) Error() string {
	var parts []string
	for _, v := range e.Violations {
		parts = append(parts, v.Field+": "+v.Message)
	}
	return "schema validation failed: " + strings.Join(parts, "; ")
}

// Validate checks a decoded JSON value (from json.Unmarshal into any) or raw bytes against loc.
func Validate(loc string, v any) error {
	s, err := Compile(loc)
	if err != nil {
		return err
	}
	var inst any
	switch b := v.(type) {
	case []byte:
		inst, err = jsonschema.UnmarshalJSON(bytes.NewReader(b))
		if err != nil {
			return &Error{Violations: []Violation{{Field: "", Rule: "json", Message: err.Error()}}}
		}
	default:
		jb, err := json.Marshal(v)
		if err != nil {
			return err
		}
		inst, _ = jsonschema.UnmarshalJSON(bytes.NewReader(jb))
	}
	if err := s.Validate(inst); err != nil {
		var ve *jsonschema.ValidationError
		if errors.As(err, &ve) {
			return &Error{Violations: flatten(ve)}
		}
		return err
	}
	return nil
}

func flatten(ve *jsonschema.ValidationError) []Violation {
	var out []Violation
	var walk func(e *jsonschema.ValidationError)
	walk = func(e *jsonschema.ValidationError) {
		if len(e.Causes) == 0 {
			field := strings.Join(e.InstanceLocation, ".")
			rule := fmt.Sprintf("%T", e.ErrorKind)
			rule = strings.TrimPrefix(rule, "*kind.")
			out = append(out, Violation{Field: field, Rule: strings.ToLower(rule), Message: e.ErrorKind.LocalizedString(printer)})
			return
		}
		for _, c := range e.Causes {
			walk(c)
		}
	}
	walk(ve)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Field < out[j].Field })
	if len(out) > 50 {
		out = out[:50]
	}
	return out
}

// Inlined returns the subschema at loc with every $ref resolved inline (for Claude Code --json-schema,
// which needs a self-contained schema; cli-invocation.md §2.1 "$refs inlined").
func Inlined(loc string) (map[string]any, error) {
	once.Do(load)
	if initErr != nil {
		return nil, initErr
	}
	base, frag, _ := strings.Cut(loc, "#")
	node, err := pointer(docs[base], frag)
	if err != nil {
		return nil, err
	}
	out, err := inline(node, base, 0)
	if err != nil {
		return nil, err
	}
	m, ok := out.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s is not an object schema", loc)
	}
	delete(m, "$id")
	m["$schema"] = "http://json-schema.org/draft-07/schema#"
	return m, nil
}

func pointer(doc any, frag string) (any, error) {
	if doc == nil {
		return nil, errors.New("unknown schema document")
	}
	if frag == "" || frag == "/" {
		return doc, nil
	}
	cur := doc
	for _, p := range strings.Split(strings.TrimPrefix(frag, "/"), "/") {
		p = strings.ReplaceAll(strings.ReplaceAll(p, "~1", "/"), "~0", "~")
		m, ok := cur.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("bad pointer %s", frag)
		}
		cur, ok = m[p]
		if !ok {
			return nil, fmt.Errorf("pointer %s not found", frag)
		}
	}
	return cur, nil
}

func inline(node any, base string, depth int) (any, error) {
	if depth > 40 {
		return nil, errors.New("ref depth exceeded")
	}
	switch n := node.(type) {
	case map[string]any:
		if ref, ok := n["$ref"].(string); ok {
			rbase, frag, _ := strings.Cut(ref, "#")
			if rbase == "" {
				rbase = base
			}
			target, err := pointer(docs[rbase], frag)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", ref, err)
			}
			res, err := inline(target, rbase, depth+1)
			if err != nil {
				return nil, err
			}
			// merge sibling keywords (description etc.)
			if rm, ok := res.(map[string]any); ok && len(n) > 1 {
				cp := map[string]any{}
				for k, v := range rm {
					cp[k] = v
				}
				for k, v := range n {
					if k != "$ref" {
						iv, err := inline(v, base, depth+1)
						if err != nil {
							return nil, err
						}
						cp[k] = iv
					}
				}
				return cp, nil
			}
			return res, nil
		}
		out := map[string]any{}
		for k, v := range n {
			if k == "$id" || k == "$schema" || k == "$defs" || k == "$comment" {
				continue
			}
			iv, err := inline(v, base, depth+1)
			if err != nil {
				return nil, err
			}
			out[k] = iv
		}
		return out, nil
	case []any:
		out := make([]any, len(n))
		for i, v := range n {
			iv, err := inline(v, base, depth+1)
			if err != nil {
				return nil, err
			}
			out[i] = iv
		}
		return out, nil
	default:
		return node, nil
	}
}

// File returns an embedded file by name.
func File(name string) ([]byte, error) { return files.ReadFile("files/" + name) }

var printer = message.NewPrinter(language.English)

// LocalRelaxed drops the violations that only exist because local images are referenced by tag, not digest
// (parity P-31: "no registry digest locally"): agent.cliImage.image. It returns nil when nothing else remains.
func LocalRelaxed(err error) error {
	var se *Error
	if !errors.As(err, &se) {
		return err
	}
	var keep []Violation
	for _, v := range se.Violations {
		if v.Field == "agent.cliImage.image" && strings.HasPrefix(v.Message, "'localhost/") {
			continue
		}
		keep = append(keep, v)
	}
	if len(keep) == 0 {
		return nil
	}
	return &Error{Violations: keep}
}
