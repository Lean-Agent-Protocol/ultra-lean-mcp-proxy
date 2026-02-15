# LAP Format Specification

> Note: This spec is now maintained by ultra-lean-mcp-core and consumed by ultra-lean-mcp.

**Version 0.1**

LAP is a compact, line-oriented format for describing tool interfaces. It is designed to be human-readable, LLM-native, and losslessly round-trippable with JSON Schema.

## Design Principles

1. **One directive per line** ג€” easy to parse, easy to scan
2. **Types are terse** ג€” `str` not `"type": "string"`
3. **Required is default** ג€” optional params are explicitly marked
4. **Descriptions inline** ג€” no nested structure needed

## Grammar

```
document     = header? tool_block+
header       = "# " server_name NL ("# " server_desc NL)?
tool_block   = version_line tool_line desc_line? param_line* output_line* example_block*
version_line = "@lap " version
tool_line    = "@tool " tool_name
desc_line    = "@desc " text
param_line   = "@in " param_def | "@opt " param_def
output_line  = "@out " output_def
example_block= "@example" text? NL ("  > " text NL)? ("  < " text NL)?

param_def    = name ":" type optional? enum? default? (" " description)?
output_def   = name ":" type ("{" output_def ("," output_def)* "}")? (" " description)?
optional     = "?"
enum         = "(" value ("/" value)* ")"
default      = "=" value

version      = "v" DIGIT+ "." DIGIT+
name         = [a-zA-Z_][a-zA-Z0-9_.-]*
type         = base_type | "[" base_type "]"
base_type    = "str" | "int" | "float" | "num" | "bool" | "obj" | "map" | "list" | "any" | "null"
```

## Types

| LAP | JSON Schema | Notes |
|----------|-------------|-------|
| `str` | `"type": "string"` | |
| `int` | `"type": "integer"` | |
| `float` / `num` | `"type": "number"` | `num` is an alias |
| `bool` | `"type": "boolean"` | |
| `obj` / `map` | `"type": "object"` | `map` is an alias |
| `list` | `"type": "array"` | Untyped array |
| `[str]` | `"type": "array", "items": {"type": "string"}` | Typed array |
| `[int]` | `"type": "array", "items": {"type": "integer"}` | Typed array |
| `any` | (no type constraint) | |
| `null` | `"type": "null"` | |

### Enums

Enums are specified inline in the type: `str(asc/desc/relevance)`

This maps to `"type": "string", "enum": ["asc", "desc", "relevance"]`.

## Directives

### `@lap`

Version header. Must appear before each `@tool` block.

```
@lap v0.1
```

### `@tool`

Declares a new tool. The name must match the MCP tool name exactly.

```
@tool create_or_update_file
```

### `@desc`

Tool description. Single line.

```
@desc Create or update a single file in a GitHub repository.
```

### `@in`

Required input parameter: `@in name:type description`

```
@in owner:str Repository owner (username or organization)
@in query:str Search query (supports GitHub search syntax)
@in tags:[str] Array of tag names
```

### `@opt`

Optional input parameter: `@opt name:type? description`

The `?` suffix on the type is redundant when using `@opt` but included for clarity.

```
@opt branch:str? Branch name (defaults to repo default)
@opt page:int? Page number for pagination (default: 1)
```

### Default values

Use `=value` after the type:

```
@opt perPage:int?=30 Results per page
@opt format:str(json/csv)?=json Output format
```

### `@out`

Output field description (optional, for documentation):

```
@out id:int The created resource ID
@out result:obj{name:str, count:int} Nested result object
```

### `@err`

Error condition (optional, for documentation):

```
@err 404 Repository not found
@err 403 Insufficient permissions
```

### `@example`

Usage example with input/output:

```
@example Create a new file
  > {"owner": "octocat", "repo": "hello", "path": "README.md", "content": "# Hello", "message": "init"}
  < {"sha": "abc123", "path": "README.md"}
```

## Required vs Optional

- **`@in`** = required parameter (no `?` suffix)
- **`@opt`** = optional parameter (`?` suffix)
- Parameters without `?` in `@in` lines are always required
- Default values imply optional

## Nested Objects

For complex nested structures, use inline notation:

```
@out result:obj{name:str, items:[obj], total:int}
```

Or describe the shape in the description:

```
@in config:obj Configuration object with keys: theme (str), lang (str), debug (bool)
```

## Bundles (Multiple Tools)

A bundle starts with `#` header lines for the server, followed by tool blocks:

```
# github
# MCP server for GitHub API integration

@lap v0.1
@tool create_or_update_file
@desc Create or update a file in a repository.
@in owner:str Repository owner
@in repo:str Repository name
...

@lap v0.1
@tool search_repositories
@desc Search for GitHub repositories.
@in query:str Search query
@opt page:int? Page number
@opt perPage:int? Results per page
```

## Escape Rules

- No escaping needed for typical tool descriptions
- Newlines within descriptions are not supported (use single line)
- If a description contains `@` at the start, it won't conflict (only `@directive` at line start is parsed)
- Colons in descriptions are fine ג€” only `name:type` at the start of `@in`/`@opt` is parsed as a parameter

## Lean Mode

Lean mode omits all descriptions, keeping only the structural information:

```
@lap v0.1
@tool create_or_update_file
@in owner:str
@in repo:str
@in path:str
@in content:str
@in message:str
@opt branch:str?
@opt sha:str?
```

This is useful when the LLM already knows the tools and only needs a structural reminder, achieving up to **63% token savings**.

