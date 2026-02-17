/**
 * Definition compression for Ultra Lean MCP Proxy (Node.js port).
 *
 * Port of the Python compression rules from ultra-lean-mcp-core.
 * Zero dependencies - uses built-in RegExp only.
 */

// ---------------------------------------------------------------------------
// Compression rules: [pattern, replacement]
// Patterns use the "gi" flags (global, case-insensitive) to match Python's
// re.IGNORECASE behaviour. Word-boundary \b works the same in JS.
// ---------------------------------------------------------------------------

const COMPRESSION_RULES = [
  // Remove filler phrases
  [/\bThis tool (?:will |can |is used to |enables (?:you|users|LLMs|AI assistants) to |allows (?:you|users|LLMs|AI assistants) to )/gi, ''],
  [/\bThis server (?:enables|allows|provides)\b/gi, ''],
  [/\bThis operation (?:will|can)\b/gi, ''],
  [/\bYou can use this (?:tool |to )\b/gi, ''],
  [/\bProvides? (?:the )?ability to\b/gi, ''],
  [/\bProvides? access to\b/gi, 'Access'],
  [/\bGives? (?:you )?access to\b/gi, 'Access'],
  [/\bmust be provided\b/gi, 'required'],
  [/\bshould be provided\b/gi, 'recommended'],
  [/\bcan be used (?:to |for )\b/gi, 'for '],
  [/\bEnables you to\b/gi, ''],
  [/\bAllows you to\b/gi, ''],

  // Simplify phrases
  [/\bin order to\b/gi, 'to'],
  [/\bas well as\b/gi, 'and'],
  [/\bprior to\b/gi, 'before'],
  [/\bwith respect to\b/gi, 'for'],

  // Remove qualifiers
  [/\bvery\b/gi, ''],
  [/\bsimply\b/gi, ''],
  [/\bbasically\b/gi, ''],
  [/\bessentially\b/gi, ''],

  // Shorten terms
  [/\brepository\b/gi, 'repo'],
  [/\bconfiguration\b/gi, 'config'],
  [/\binformation\b/gi, 'info'],
  [/\bdocumentation\b/gi, 'docs'],
  [/\bapplication\b/gi, 'app'],
  [/\bdatabase\b/gi, 'DB'],
  [/\benvironment\b/gi, 'env'],
  [/\bparameters\b/gi, 'params'],
  [/\bparameter\b/gi, 'param'],

  // Shorten verbs
  [/\bretrieve(?:s)?\b/gi, 'get'],
  [/\bfetch(?:es)?\b/gi, 'get'],
  [/\bexecute(?:s)?\b/gi, 'run'],
  [/\bgenerate(?:s)?\b/gi, 'create'],

  // Shorten notes
  [/\bfor example\b/gi, 'e.g.'],
  [/\bsuch as\b/gi, 'like'],

  // Clean up whitespace and punctuation
  [/  +/g, ' '],
  [/ +([.,;:])/g, '$1'],
  [/^\s+|\s+$/g, ''],
];

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Compress a tool or parameter description string.
 *
 * Returns the original string untouched when it is falsy or shorter than 20
 * characters (too short to benefit from compression).
 *
 * @param {string|null|undefined} desc
 * @returns {string|null|undefined}
 */
export function compressDescription(desc) {
  if (!desc || desc.length < 20) {
    return desc;
  }

  let result = desc;

  for (const [pattern, replacement] of COMPRESSION_RULES) {
    // Reset lastIndex for global regexes to ensure a clean match each time
    pattern.lastIndex = 0;
    result = result.replace(pattern, replacement);
  }

  // Collapse repeated dots
  result = result.replace(/\.+/g, '.');

  // Capitalise first letter after ". "
  result = result.replace(/(\. )([a-z])/g, (_match, dot, letter) => dot + letter.toUpperCase());

  // Capitalise very first character
  if (result && result[0] === result[0].toLowerCase()) {
    result = result[0].toUpperCase() + result.slice(1);
  }

  return result.trim();
}

/**
 * Recursively compress description fields inside a JSON Schema object.
 *
 * Mutates the schema in-place and returns it for convenience.
 *
 * @param {object} schema
 * @returns {object}
 */
export function compressSchema(schema) {
  if (typeof schema !== 'object' || schema === null) {
    return schema;
  }

  if (typeof schema.description === 'string') {
    schema.description = compressDescription(schema.description);
  }

  if (schema.properties && typeof schema.properties === 'object') {
    for (const key of Object.keys(schema.properties)) {
      const propSchema = schema.properties[key];
      if (typeof propSchema === 'object' && propSchema !== null) {
        compressSchema(propSchema);
      }
    }
  }

  if (schema.items && typeof schema.items === 'object') {
    compressSchema(schema.items);
  }

  return schema;
}

/**
 * Compress an entire MCP tools list (the `tools` array from tools/list).
 *
 * Each tool entry is expected to have `description` and `inputSchema` fields.
 * The function mutates entries in-place and returns the array.
 *
 * @param {Array<object>} tools
 * @returns {Array<object>}
 */
export function compressManifest(tools) {
  if (!Array.isArray(tools)) {
    return tools;
  }

  for (const tool of tools) {
    if (typeof tool !== 'object' || tool === null) {
      continue;
    }

    if (typeof tool.description === 'string') {
      tool.description = compressDescription(tool.description);
    }

    if (tool.inputSchema && typeof tool.inputSchema === 'object') {
      compressSchema(tool.inputSchema);
    }
  }

  return tools;
}
