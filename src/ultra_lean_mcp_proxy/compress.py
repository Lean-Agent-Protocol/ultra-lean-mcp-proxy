"""
Description text compression — rule-based compression for verbose MCP tool
descriptions to reduce token usage while preserving semantic meaning.
"""

import re
from typing import Any, Dict, List

# Compression rules: (pattern, replacement) applied in order
COMPRESSION_RULES = [
    # Remove filler phrases
    (r'\bThis tool (?:will |can |is used to |enables (?:you|users|LLMs|AI assistants) to |allows (?:you|users|LLMs|AI assistants) to )', r''),
    (r'\bThis server (?:enables|allows|provides)\b', r''),
    (r'\bThis operation (?:will|can)\b', r''),
    (r'\bYou can use this (?:tool |to )\b', r''),
    (r'\bProvides? (?:the )?ability to\b', r''),
    (r'\bProvides? access to\b', r'Access'),
    (r'\bGives? (?:you )?access to\b', r'Access'),
    (r'\bmust be provided\b', r'required'),
    (r'\bshould be provided\b', r'recommended'),
    (r'\bcan be used (?:to |for )\b', r'for '),
    (r'\bEnables you to\b', r''),
    (r'\bAllows you to\b', r''),
    # Simplify phrases
    (r'\bin order to\b', r'to'),
    (r'\bas well as\b', r'and'),
    (r'\bprior to\b', r'before'),
    (r'\bwith respect to\b', r'for'),
    # Remove qualifiers
    (r'\bvery\b', r''),
    (r'\bsimply\b', r''),
    (r'\bbasically\b', r''),
    (r'\bessentially\b', r''),
    # Shorten terms
    (r'\brepository\b', r'repo'),
    (r'\bconfiguration\b', r'config'),
    (r'\binformation\b', r'info'),
    (r'\bdocumentation\b', r'docs'),
    (r'\bapplication\b', r'app'),
    (r'\bdatabase\b', r'DB'),
    (r'\benvironment\b', r'env'),
    (r'\bparameters\b', r'params'),
    (r'\bparameter\b', r'param'),
    # Shorten verbs
    (r'\bretrieve(?:s)?\b', r'get'),
    (r'\bfetch(?:es)?\b', r'get'),
    (r'\bexecute(?:s)?\b', r'run'),
    (r'\bgenerate(?:s)?\b', r'create'),
    # Shorten notes
    (r'\bfor example\b', r'e.g.'),
    (r'\bsuch as\b', r'like'),
    # Clean up
    (r'  +', r' '),
    (r' +([.,;:])', r'\1'),
    (r'^\s+|\s+$', r''),
]


def compress_description(desc: str) -> str:
    """Apply rule-based compression to a tool description."""
    if not desc or len(desc) < 20:
        return desc
    result = desc
    for pattern, replacement in COMPRESSION_RULES:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = re.sub(r'\.+', '.', result)
    result = re.sub(r'(\. )([a-z])', lambda m: m.group(1) + m.group(2).upper(), result)
    if result and result[0].islower():
        result = result[0].upper() + result[1:]
    return result.strip()


def compress_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively compress description fields in a JSON Schema."""
    if not isinstance(schema, dict):
        return schema
    if 'description' in schema:
        schema['description'] = compress_description(schema['description'])
    if 'properties' in schema:
        for prop_schema in schema['properties'].values():
            if isinstance(prop_schema, dict):
                compress_schema(prop_schema)
    if 'items' in schema and isinstance(schema['items'], dict):
        compress_schema(schema['items'])
    return schema


def compress_manifest(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compress a full MCP tools/list response."""
    result = []
    for tool in tools:
        t = tool.copy()
        if 'description' in t:
            t['description'] = compress_description(t['description'])
        if 'inputSchema' in t:
            t['inputSchema'] = compress_schema(t['inputSchema'].copy())
        result.append(t)
    return result
