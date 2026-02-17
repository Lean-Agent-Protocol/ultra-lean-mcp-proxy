/**
 * Ultra Lean MCP Proxy - stdio proxy with composable v2 optimization pipeline.
 */

import { spawn } from 'node:child_process';
import { compressDescription, compressSchema } from './compress.mjs';
import { loadProxyConfig, featureEnabledForTool, cacheTtlForTool } from './config.mjs';
import { ProxyState, cloneJson, isMutatingToolName, makeCacheKey } from './state.mjs';
import { createDelta, stableHash } from './delta.mjs';
import {
  TokenCounter,
  makeCompressionOptions,
  compressResult,
  estimateCompressibility,
  tokenSavings,
} from './result-compression.mjs';
import { computeToolsHash, parseIfNoneMatch } from './tools-hash-sync.mjs';

const SEARCH_TOOL_NAME = 'ultra_lean_mcp_proxy.search_tools';

function createLineReader(stream, onLine) {
  let buffer = '';
  stream.on('data', (chunk) => {
    buffer += chunk.toString('utf-8');
    let idx;
    while ((idx = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) onLine(line);
    }
  });
  stream.on('end', () => {
    const remaining = buffer.trim();
    if (remaining) onLine(remaining);
  });
}

function jsonSize(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf-8');
}

function runtimeMetricsSnapshot(metrics) {
  return {
    upstream_requests: Number(metrics.upstreamRequests),
    upstream_request_tokens: Number(metrics.upstreamRequestTokens),
    upstream_request_bytes: Number(metrics.upstreamRequestBytes),
    upstream_responses: Number(metrics.upstreamResponses),
    upstream_response_tokens: Number(metrics.upstreamResponseTokens),
    upstream_response_bytes: Number(metrics.upstreamResponseBytes),
  };
}

function featureHealthKey(feature, toolName) {
  return `${feature}:${toolName || '_global'}`;
}

function featureIsActive(featureStates, key, cfg) {
  if (!cfg.autoDisableEnabled) return true;
  const state = featureStates[key] || { regressionStreak: 0, cooldownRemaining: 0 };
  featureStates[key] = state;
  if (state.cooldownRemaining > 0) {
    state.cooldownRemaining -= 1;
    return false;
  }
  return true;
}

function recordFeatureOutcome(featureStates, key, outcome, cfg) {
  if (!cfg.autoDisableEnabled) return;
  const state = featureStates[key] || { regressionStreak: 0, cooldownRemaining: 0 };
  featureStates[key] = state;
  if (outcome === 'success') {
    state.regressionStreak = 0;
    return;
  }
  if (outcome === 'neutral') {
    state.regressionStreak = Math.max(0, state.regressionStreak - 1);
    return;
  }
  if (outcome === 'hurt') {
    state.regressionStreak += 1;
    if (state.regressionStreak >= cfg.autoDisableThreshold) {
      state.regressionStreak = 0;
      state.cooldownRemaining = cfg.autoDisableCooldownRequests;
    }
  }
}

function extractToolCall(msg) {
  const params = msg?.params;
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    return [null, {}];
  }
  const name = typeof params.name === 'string' ? params.name : null;
  const argumentsValue = params.arguments;
  const argumentsObj = argumentsValue && typeof argumentsValue === 'object' && !Array.isArray(argumentsValue)
    ? argumentsValue
    : {};
  return [name, argumentsObj];
}

function clientSupportsToolsHashSync(params) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) return false;
  const caps = params.capabilities;
  if (!caps || typeof caps !== 'object' || Array.isArray(caps)) return false;
  const experimental = caps.experimental;
  if (!experimental || typeof experimental !== 'object' || Array.isArray(experimental)) return false;
  const proxyExt = experimental.ultra_lean_mcp_proxy;
  if (!proxyExt || typeof proxyExt !== 'object' || Array.isArray(proxyExt)) return false;
  const toolsHashSync = proxyExt.tools_hash_sync;
  if (!toolsHashSync || typeof toolsHashSync !== 'object' || Array.isArray(toolsHashSync)) return false;
  const version = toolsHashSync.version;
  if (typeof version === 'number') return version === 1;
  if (typeof version === 'string') return version.trim() === '1';
  return false;
}

function extractToolsHashIfNoneMatch(params, algorithm) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    return { provided: false, valid: false, value: null };
  }
  const proxyExt = params._ultra_lean_mcp_proxy;
  if (!proxyExt || typeof proxyExt !== 'object' || Array.isArray(proxyExt)) {
    return { provided: false, valid: false, value: null };
  }
  const toolsHashSync = proxyExt.tools_hash_sync;
  if (!toolsHashSync || typeof toolsHashSync !== 'object' || Array.isArray(toolsHashSync)) {
    return { provided: false, valid: false, value: null };
  }
  if (!Object.prototype.hasOwnProperty.call(toolsHashSync, 'if_none_match')) {
    return { provided: false, valid: false, value: null };
  }
  const normalized = parseIfNoneMatch(toolsHashSync.if_none_match, { expectedAlgorithm: algorithm });
  if (!normalized) {
    return { provided: true, valid: false, value: null };
  }
  return { provided: true, valid: true, value: normalized };
}

function injectInitializeToolsHashCapability(result, algorithm) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return result;
  const out = cloneJson(result);
  if (!out.capabilities || typeof out.capabilities !== 'object' || Array.isArray(out.capabilities)) {
    out.capabilities = {};
  }
  if (
    !out.capabilities.experimental
    || typeof out.capabilities.experimental !== 'object'
    || Array.isArray(out.capabilities.experimental)
  ) {
    out.capabilities.experimental = {};
  }
  if (
    !out.capabilities.experimental.ultra_lean_mcp_proxy
    || typeof out.capabilities.experimental.ultra_lean_mcp_proxy !== 'object'
    || Array.isArray(out.capabilities.experimental.ultra_lean_mcp_proxy)
  ) {
    out.capabilities.experimental.ultra_lean_mcp_proxy = {};
  }
  out.capabilities.experimental.ultra_lean_mcp_proxy.tools_hash_sync = {
    version: 1,
    algorithm,
  };
  return out;
}

function toolsHashScopeKey(cfg, profileFingerprint) {
  return `${cfg.sessionId}:${cfg.serverName}:${profileFingerprint}`;
}

function buildProfileFingerprint(cfg, upstreamCommand) {
  return stableHash({
    server_name: cfg.serverName,
    command: upstreamCommand.join(' '),
  });
}

function buildSearchToolDefinition(toolNames = null) {
  const baseDesc = 'Search available tools and return full schemas on demand.';
  let description = baseDesc;
  if (toolNames && toolNames.length > 0) {
    description = baseDesc
      + ' Use "select:<tool_name>" for direct selection, or keywords to search.\n\n'
      + 'Available tools (must be loaded via this tool before use):\n'
      + toolNames.join('\n');
  }
  return {
    name: SEARCH_TOOL_NAME,
    description,
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search query' },
        server: { type: 'string', description: 'Optional server name' },
        top_k: { type: 'integer', description: 'Max number of results', default: 8 },
        include_schemas: {
          type: 'boolean',
          description: 'Include inputSchema in matches',
          default: false,
        },
      },
      required: ['query'],
    },
  };
}

function applyDefinitionCompression(tools) {
  const out = [];
  for (const tool of tools) {
    const item = cloneJson(tool);
    if (Object.prototype.hasOwnProperty.call(item, 'description')) {
      item.description = compressDescription(String(item.description));
    }
    const schema = item.inputSchema || item.input_schema;
    if (schema && typeof schema === 'object') {
      compressSchema(schema);
    }
    out.push(item);
  }
  return out;
}

function minimalTool(tool) {
  const name = tool?.name || '';
  const description = tool?.description || '';
  const schema = tool?.inputSchema || tool?.input_schema || {};
  const properties = schema && typeof schema === 'object' && !Array.isArray(schema)
    ? schema.properties || {}
    : {};
  const compactProps = {};
  if (properties && typeof properties === 'object' && !Array.isArray(properties)) {
    for (const [key, value] of Object.entries(properties)) {
      const ptype = value && typeof value === 'object' && typeof value.type === 'string'
        ? value.type
        : 'string';
      compactProps[String(key)] = { type: ptype };
    }
  }
  return {
    name,
    description,
    inputSchema: { type: 'object', properties: compactProps },
  };
}

function handleToolsListResult(
  result,
  state,
  cfg,
  metrics,
  tokenCounter,
  {
    toolsHashSyncNegotiated,
    profileFingerprint,
    ifNoneMatch = null,
    ifNoneMatchProvided = false,
    ifNoneMatchValid = false,
  } = {}
) {
  const tools = result?.tools;
  if (!Array.isArray(tools)) return result;

  metrics.toolsListRequests += 1;
  const originalSize = jsonSize(result);

  let processedTools = cloneJson(tools);
  if (cfg.definitionCompressionEnabled) {
    processedTools = applyDefinitionCompression(processedTools);
  }
  state.setTools(processedTools);

  let visibleTools = processedTools;
  let lazyAllowed = false;
  if (cfg.lazyLoadingEnabled) {
    const toolCount = processedTools.length;
    const toolTokens = tokenCounter.count({ tools: processedTools });
    lazyAllowed = toolCount >= cfg.lazyMinTools || toolTokens >= cfg.lazyMinTokens;
  }
  if (lazyAllowed) {
    if (cfg.lazyMode === 'search_only') {
      visibleTools = [];
    } else if (cfg.lazyMode === 'catalog') {
      visibleTools = processedTools.map((t) => ({ name: t.name, inputSchema: { type: 'object' } }));
    } else if (cfg.lazyMode === 'minimal') {
      visibleTools = processedTools.map((tool) => minimalTool(tool));
    }
    const toolNames = cfg.lazyMode === 'catalog'
      ? processedTools.map((t) => t.name)
      : null;
    visibleTools = [...visibleTools, buildSearchToolDefinition(toolNames)];
  }

  const out = cloneJson(result);
  out.tools = visibleTools;
  const compressedSize = jsonSize(out);
  const saved = originalSize - compressedSize;
  if (saved > 0) {
    metrics.toolsListSavedBytes += saved;
  }

  if (!(cfg.toolsHashSyncEnabled && toolsHashSyncNegotiated)) {
    return out;
  }

  const scopeKey = toolsHashScopeKey(cfg, profileFingerprint);
  try {
    const toolsHash = computeToolsHash(visibleTools, {
      algorithm: cfg.toolsHashSyncAlgorithm,
      includeServerFingerprint: cfg.toolsHashSyncIncludeServerFingerprint,
      serverFingerprint: profileFingerprint,
    });
    state.toolsHashSetLast(scopeKey, toolsHash);

    const conditionalMatch = Boolean(ifNoneMatchValid && ifNoneMatch === toolsHash);
    if (conditionalMatch) {
      const hitCount = state.toolsHashRecordHit(scopeKey);
      metrics.toolsHashSyncHits += 1;
      const forceRefresh = (hitCount % cfg.toolsHashSyncRefreshInterval) === 0;
      if (!forceRefresh) {
        const notModified = cloneJson(out);
        notModified.tools = [];
        if (!notModified._ultra_lean_mcp_proxy || typeof notModified._ultra_lean_mcp_proxy !== 'object') {
          notModified._ultra_lean_mcp_proxy = {};
        }
        notModified._ultra_lean_mcp_proxy.tools_hash_sync = {
          not_modified: true,
          tools_hash: toolsHash,
        };
        metrics.toolsHashSyncNotModified += 1;
        const byteDelta = Math.max(0, jsonSize(out) - jsonSize(notModified));
        if (byteDelta > 0) metrics.toolsHashSyncSavedBytes += byteDelta;
        const tokenDelta = Math.max(0, tokenCounter.count(out) - tokenCounter.count(notModified));
        if (tokenDelta > 0) metrics.toolsHashSyncSavedTokens += tokenDelta;
        return notModified;
      }
    } else if (ifNoneMatchProvided && ifNoneMatchValid) {
      metrics.toolsHashSyncMisses += 1;
    }

    state.toolsHashResetHits(scopeKey);
    if (!out._ultra_lean_mcp_proxy || typeof out._ultra_lean_mcp_proxy !== 'object') {
      out._ultra_lean_mcp_proxy = {};
    }
    out._ultra_lean_mcp_proxy.tools_hash_sync = {
      not_modified: false,
      tools_hash: toolsHash,
    };
    return out;
  } catch {
    return out;
  }
}

function buildSearchResult(state, cfg, argumentsValue) {
  const query = String(argumentsValue?.query || '').trim();
  const topKRaw = argumentsValue?.top_k ?? cfg.lazyTopK;
  const includeSchemas = Boolean(argumentsValue?.include_schemas);
  const parsedTopK = Number.parseInt(String(topKRaw), 10);
  const topK = Number.isFinite(parsedTopK) && parsedTopK > 0 ? parsedTopK : cfg.lazyTopK;

  const matches = state.searchTools(query, topK, includeSchemas);
  const topScore = matches.length > 0 ? Number(matches[0].score || 0) : 0;
  const payload = {
    server: cfg.serverName,
    query,
    count: matches.length,
    matches,
  };
  if (cfg.lazyFallbackFullOnLowConfidence && topScore < cfg.lazyMinConfidenceScore) {
    payload.fallback = 'full_tools_due_low_confidence';
    payload.top_score = topScore;
    payload.tools = state.getTools();
  }
  return {
    structuredContent: payload,
    content: [{ type: 'text', text: JSON.stringify(payload) }],
  };
}

function toolCacheAllowed(cfg, toolName) {
  if (!toolName || !cfg.cachingEnabled) return false;
  if (!featureEnabledForTool(cfg, toolName, 'caching', true)) return false;
  if (!cfg.cacheMutatingTools && isMutatingToolName(toolName)) return false;
  return true;
}

function minifyRedundantTextContent(content, originalPayload) {
  const kept = [];
  let removed = 0;
  for (const item of content) {
    if (!item || typeof item !== 'object' || item.type !== 'text') {
      kept.push(item);
      continue;
    }
    const text = item.text;
    if (typeof text !== 'string') {
      kept.push(item);
      continue;
    }
    const trimmed = text.trim();
    if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) {
      kept.push(item);
      continue;
    }
    try {
      const parsed = JSON.parse(trimmed);
      if (JSON.stringify(parsed) === JSON.stringify(originalPayload)) {
        removed += 1;
        continue;
      }
    } catch {
      // keep on parse errors
    }
    kept.push(item);
  }
  if (removed > 0 && kept.length === 0) {
    kept.push({ type: 'text', text: '[ultra-lean-mcp-proxy] structured result' });
  }
  return { content: kept, changed: removed > 0 };
}

function applyResultCompression(
  result,
  toolName,
  cfg,
  metrics,
  tokenCounter,
  featureStates,
  keyRegistry,
  keyRegistryCounter
) {
  if (!cfg.resultCompressionEnabled) return result;
  if (!featureEnabledForTool(cfg, toolName, 'result_compression', true)) return result;

  const featureKey = featureHealthKey('result_compression', toolName);
  if (!featureIsActive(featureStates, featureKey, cfg)) return result;

  const options = makeCompressionOptions({
    mode: cfg.resultCompressionMode,
    stripNulls: cfg.resultStripNulls,
    stripDefaults: cfg.resultStripDefaults,
    minPayloadBytes: cfg.resultMinPayloadBytes,
  });

  let outcome = 'neutral';
  try {
    if (
      result
      && typeof result === 'object'
      && !Array.isArray(result)
      && (Array.isArray(result.structuredContent) || (
        result.structuredContent
        && typeof result.structuredContent === 'object'
      ))
    ) {
      const out = cloneJson(result);
      const original = out.structuredContent;
      if (estimateCompressibility(original) < cfg.resultMinCompressibility) {
        recordFeatureOutcome(featureStates, featureKey, 'neutral', cfg);
        return result;
      }
      const env = compressResult(original, options, {
        keyRegistry,
        registryCounter: keyRegistryCounter,
        reuseKeys: cfg.resultSharedKeyRegistry,
        keyBootstrapInterval: cfg.resultKeyBootstrapInterval,
      });
      if (env.compressed) {
        const tokenDelta = tokenSavings(original, env, tokenCounter);
        const minRequired = Math.max(
          cfg.resultMinTokenSavingsAbs,
          Math.floor(tokenCounter.count(original) * cfg.resultMinTokenSavingsRatio)
        );
        if (tokenDelta >= minRequired) {
          out.structuredContent = env;
          if (!out._ultra_lean_mcp_proxy || typeof out._ultra_lean_mcp_proxy !== 'object') {
            out._ultra_lean_mcp_proxy = {};
          }
          out._ultra_lean_mcp_proxy.result_compression = {
            saved_bytes: env.savedBytes || 0,
            saved_ratio: env.savedRatio || 0,
            saved_tokens: tokenDelta,
          };
          metrics.resultCompressions += 1;
          metrics.resultSavedBytes += Number(env.savedBytes || 0);
          outcome = 'success';
          if (cfg.resultMinifyRedundantText && Array.isArray(out.content)) {
            const minified = minifyRedundantTextContent(out.content, original);
            if (minified.changed) {
              out.content = minified.content;
            }
          }
          recordFeatureOutcome(featureStates, featureKey, outcome, cfg);
          return out;
        }
        if (tokenDelta < 0) outcome = 'hurt';
      }
      recordFeatureOutcome(featureStates, featureKey, outcome, cfg);
      return result;
    }

    if (result && typeof result === 'object' && !Array.isArray(result) && Array.isArray(result.content)) {
      const out = cloneJson(result);
      let changed = false;
      let totalSavedBytes = 0;
      let totalSavedTokens = 0;
      for (const item of out.content) {
        if (!item || typeof item !== 'object' || item.type !== 'text') continue;
        if (typeof item.text !== 'string') continue;
        const trimmed = item.text.trim();
        if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) continue;
        let parsed;
        try {
          parsed = JSON.parse(trimmed);
        } catch {
          continue;
        }
        if (estimateCompressibility(parsed) < cfg.resultMinCompressibility) continue;
        const env = compressResult(parsed, options, {
          keyRegistry,
          registryCounter: keyRegistryCounter,
          reuseKeys: cfg.resultSharedKeyRegistry,
          keyBootstrapInterval: cfg.resultKeyBootstrapInterval,
        });
        if (!env.compressed) continue;
        const tokenDelta = tokenSavings(parsed, env, tokenCounter);
        const minRequired = Math.max(
          cfg.resultMinTokenSavingsAbs,
          Math.floor(tokenCounter.count(parsed) * cfg.resultMinTokenSavingsRatio)
        );
        if (tokenDelta >= minRequired) {
          item.text = JSON.stringify(env);
          changed = true;
          totalSavedBytes += Number(env.savedBytes || 0);
          totalSavedTokens += tokenDelta;
          outcome = 'success';
        } else if (tokenDelta < 0 && outcome !== 'success') {
          outcome = 'hurt';
        }
      }
      if (changed) {
        if (!out._ultra_lean_mcp_proxy || typeof out._ultra_lean_mcp_proxy !== 'object') {
          out._ultra_lean_mcp_proxy = {};
        }
        out._ultra_lean_mcp_proxy.result_compression = {
          saved_bytes: totalSavedBytes,
          saved_tokens: totalSavedTokens,
        };
        metrics.resultCompressions += 1;
        metrics.resultSavedBytes += totalSavedBytes;
        recordFeatureOutcome(featureStates, featureKey, 'success', cfg);
        return out;
      }
      recordFeatureOutcome(featureStates, featureKey, outcome, cfg);
      return result;
    }
  } catch {
    recordFeatureOutcome(featureStates, featureKey, 'neutral', cfg);
    return result;
  }

  recordFeatureOutcome(featureStates, featureKey, 'neutral', cfg);
  return result;
}

function applyDeltaResponse(result, historyKey, toolName, state, cfg, metrics, deltaCounters, tokenCounter) {
  const previous = state.historyGet(historyKey);
  state.historySet(historyKey, result);

  if (!cfg.deltaResponsesEnabled) return result;
  if (!featureEnabledForTool(cfg, toolName, 'delta_responses', true)) return result;
  if (previous === null) {
    deltaCounters[historyKey] = 0;
    return result;
  }
  if ((deltaCounters[historyKey] || 0) >= cfg.deltaSnapshotInterval) {
    deltaCounters[historyKey] = 0;
    return result;
  }

  const fullTokens = tokenCounter.count(result);

  if (JSON.stringify(previous) === JSON.stringify(result)) {
    const delta = {
      encoding: 'lapc-delta-v1',
      unchanged: true,
      currentHash: stableHash(result),
    };
    const payload = { delta };
    if (tokenCounter.count(payload) >= fullTokens) return result;
    deltaCounters[historyKey] = (deltaCounters[historyKey] || 0) + 1;
    metrics.deltaResponses += 1;
    metrics.deltaSavedBytes += Math.max(0, jsonSize(result) - jsonSize(payload));
    return {
      structuredContent: payload,
      content: [{ type: 'text', text: JSON.stringify(payload) }],
    };
  }

  try {
    const delta = createDelta(previous, result, cfg.deltaMinSavingsRatio, cfg.deltaMaxPatchBytes);
    if (!delta) return result;
    const patchRatio = Number(delta.fullBytes || 0) > 0
      ? Number(delta.patchBytes || 0) / Number(delta.fullBytes || 1)
      : 0;
    if (patchRatio > cfg.deltaMaxPatchRatio) return result;
    const payload = { delta };
    if (tokenCounter.count(payload) >= fullTokens) return result;
    deltaCounters[historyKey] = (deltaCounters[historyKey] || 0) + 1;
    metrics.deltaResponses += 1;
    metrics.deltaSavedBytes += Number(delta.savedBytes || 0);
    return {
      structuredContent: payload,
      content: [{ type: 'text', text: JSON.stringify(payload) }],
    };
  } catch {
    return result;
  }
}

function resolveSpawnOptions(commandName) {
  const normalized = typeof commandName === 'string' ? commandName.trim().toLowerCase() : '';
  const isCmdShim = normalized === 'cmd' || normalized === 'cmd.exe';
  const useShellOnWindows = process.platform === 'win32'
    && typeof commandName === 'string'
    && !isCmdShim
    && !/[\\/]/.test(commandName)
    && !/^[A-Za-z]:/.test(commandName);
  return {
    stdio: ['pipe', 'pipe', 'pipe'],
    ...(useShellOnWindows ? { shell: true } : {}),
  };
}

function parseMessageLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function traceInbound(traceRpc, msg) {
  if (!traceRpc) return;
  if (msg.method) {
    const idPart = msg.id !== undefined ? ` id=${msg.id}` : '';
    const kind = msg.id !== undefined ? 'request' : 'notification';
    process.stderr.write(`[ultra-lean-mcp-proxy] rpc<- client ${kind} method=${msg.method}${idPart}\n`);
  } else if (msg.id !== undefined) {
    process.stderr.write(`[ultra-lean-mcp-proxy] rpc<- client response id=${msg.id}\n`);
  }
}

function traceUpstream(traceRpc, msg, pending) {
  if (!traceRpc) return;
  if (msg.method) {
    const idPart = msg.id !== undefined ? ` id=${msg.id}` : '';
    const kind = msg.id !== undefined ? 'request' : 'notification';
    process.stderr.write(`[ultra-lean-mcp-proxy] rpc<- upstream ${kind} method=${msg.method}${idPart}\n`);
  } else if (msg.id !== undefined) {
    const req = pending.get(msg.id);
    const methodPart = req?.method ? ` method=${req.method}` : '';
    const status = msg.error ? 'error' : 'result';
    process.stderr.write(
      `[ultra-lean-mcp-proxy] rpc<- upstream response id=${msg.id}${methodPart} status=${status}\n`
    );
  }
}

export function runProxy(upstreamCommand, options = {}) {
  const traceRpc = Boolean(options.traceRpc);
  const cfg = loadProxyConfig({
    upstreamCommand,
    configPath: options.configPath || null,
    cliOverrides: {
      stats: options.stats,
      verbose: options.verbose,
      sessionId: options.sessionId,
      strictConfig: options.strictConfig,
      resultCompression: options.resultCompression,
      deltaResponses: options.deltaResponses,
      lazyLoading: options.lazyLoading,
      toolsHashSync: options.toolsHashSync,
      caching: options.caching,
      cacheTtl: options.cacheTtl,
      deltaMinSavings: options.deltaMinSavings,
      lazyMode: options.lazyMode,
      toolsHashRefreshInterval: options.toolsHashRefreshInterval,
      searchTopK: options.searchTopK,
      resultCompressionMode: options.resultCompressionMode,
      configPath: options.configPath,
    },
  });

  if (options.stats) cfg.stats = true;

  if (options.dumpEffectiveConfig) {
    process.stderr.write(`${JSON.stringify(cfg, null, 2)}\n`);
  }

  const featureSummary = [
    `definition=${cfg.definitionCompressionEnabled ? 'on' : 'off'}`,
    `result=${cfg.resultCompressionEnabled ? 'on' : 'off'}`,
    `delta=${cfg.deltaResponsesEnabled ? 'on' : 'off'}`,
    `lazy=${cfg.lazyLoadingEnabled ? cfg.lazyMode : 'off'}`,
    `tools_hash_sync=${cfg.toolsHashSyncEnabled ? 'on' : 'off'}`,
    `cache=${cfg.cachingEnabled ? 'on' : 'off'}`,
  ].join(',');
  process.stderr.write(`[ultra-lean-mcp-proxy] runtime=npm features=${featureSummary}\n`);
  if (traceRpc) {
    process.stderr.write('[ultra-lean-mcp-proxy] trace-rpc enabled\n');
  }

  const upstream = spawn(upstreamCommand[0], upstreamCommand.slice(1), resolveSpawnOptions(upstreamCommand[0]));
  const profileFingerprint = buildProfileFingerprint(cfg, upstreamCommand);

  const pending = new Map();
  const state = new ProxyState(cfg.cacheMaxEntries);
  const tokenCounter = new TokenCounter();
  const featureStates = {};
  const deltaCounters = {};
  const keyRegistry = {};
  const keyRegistryCounter = {};
  let toolsHashSyncNegotiated = false;

  const metrics = {
    toolsListRequests: 0,
    toolsListSavedBytes: 0,
    toolsHashSyncHits: 0,
    toolsHashSyncMisses: 0,
    toolsHashSyncNotModified: 0,
    toolsHashSyncSavedBytes: 0,
    toolsHashSyncSavedTokens: 0,
    cacheHits: 0,
    cacheMisses: 0,
    resultCompressions: 0,
    resultSavedBytes: 0,
    deltaResponses: 0,
    deltaSavedBytes: 0,
    searchCalls: 0,
    upstreamRequests: 0,
    upstreamRequestBytes: 0,
    upstreamRequestTokens: 0,
    upstreamResponses: 0,
    upstreamResponseBytes: 0,
    upstreamResponseTokens: 0,
  };

  function sendToClient(msg) {
    if (cfg.stats && msg && typeof msg === 'object') {
      const result = msg.result;
      if (result && typeof result === 'object' && !Array.isArray(result)) {
        if (!result._ultra_lean_mcp_proxy || typeof result._ultra_lean_mcp_proxy !== 'object') {
          result._ultra_lean_mcp_proxy = {};
        }
        result._ultra_lean_mcp_proxy.runtime_metrics = runtimeMetricsSnapshot(metrics);
      }
    }
    process.stdout.write(`${JSON.stringify(msg)}\n`);
  }

  createLineReader(process.stdin, (line) => {
    const msg = parseMessageLine(line);
    if (!msg) {
      if (traceRpc) {
        process.stderr.write('[ultra-lean-mcp-proxy] rpc<- client non-json-line\n');
      }
      upstream.stdin.write(`${line}\n`);
      return;
    }

    traceInbound(traceRpc, msg);

    const method = msg.method;
    const reqId = msg.id;
    let shortCircuited = false;

    if (typeof method === 'string' && reqId !== undefined) {
      try {
        if (method === 'initialize') {
          pending.set(reqId, {
            method,
            clientToolsHashSyncSupported: clientSupportsToolsHashSync(msg.params),
          });
        } else if (method === 'tools/list') {
          const conditional = extractToolsHashIfNoneMatch(msg.params, cfg.toolsHashSyncAlgorithm);
          if (
            cfg.toolsHashSyncEnabled
            && toolsHashSyncNegotiated
            && conditional.valid
            && typeof conditional.value === 'string'
          ) {
            const scopeKey = toolsHashScopeKey(cfg, profileFingerprint);
            const entry = state.toolsHashGet(scopeKey);
            if (entry && entry.lastHash === conditional.value) {
              const nextHit = (entry.conditionalHits || 0) + 1;
              const forceRefresh = (nextHit % cfg.toolsHashSyncRefreshInterval) === 0;
              if (!forceRefresh) {
                state.toolsHashRecordHit(scopeKey);
                metrics.toolsHashSyncHits += 1;
                metrics.toolsHashSyncNotModified += 1;
                sendToClient({
                  jsonrpc: msg.jsonrpc || '2.0',
                  id: reqId,
                  result: {
                    tools: [],
                    _ultra_lean_mcp_proxy: {
                      tools_hash_sync: {
                        not_modified: true,
                        tools_hash: conditional.value,
                      },
                    },
                  },
                });
                shortCircuited = true;
              }
            }
          }
          if (!shortCircuited) {
            pending.set(reqId, {
              method,
              toolsHashIfNoneMatch: conditional.value,
              toolsHashIfNoneMatchProvided: conditional.provided,
              toolsHashIfNoneMatchValid: conditional.valid,
            });
          }
        } else if (method === 'tools/call') {
          const [toolName, argumentsValue] = extractToolCall(msg);

          if (cfg.lazyLoadingEnabled && toolName === SEARCH_TOOL_NAME) {
            const searchResult = buildSearchResult(state, cfg, argumentsValue);
            metrics.searchCalls += 1;
            sendToClient({
              jsonrpc: msg.jsonrpc || '2.0',
              id: reqId,
              result: searchResult,
            });
            shortCircuited = true;
          }

          if (!shortCircuited) {
            let cacheKey = null;
            if (toolCacheAllowed(cfg, toolName)) {
              cacheKey = makeCacheKey(cfg.sessionId, cfg.serverName, toolName, argumentsValue);
              const cached = state.cacheGet(cacheKey);
              if (cached !== null) {
                metrics.cacheHits += 1;
                const delivered = applyDeltaResponse(
                  cached,
                  cacheKey,
                  toolName,
                  state,
                  cfg,
                  metrics,
                  deltaCounters,
                  tokenCounter
                );
                sendToClient({
                  jsonrpc: msg.jsonrpc || '2.0',
                  id: reqId,
                  result: delivered,
                });
                shortCircuited = true;
              } else {
                metrics.cacheMisses += 1;
              }
            }

            if (!shortCircuited) {
              pending.set(reqId, {
                method,
                toolName,
                argumentsValue,
                cacheKey,
              });
            }
          }
        } else {
          pending.set(reqId, { method });
        }
      } catch {
        // fail-open
      }
    }

    if (shortCircuited) {
      return;
    }

    upstream.stdin.write(`${JSON.stringify(msg)}\n`);
    metrics.upstreamRequests += 1;
    metrics.upstreamRequestBytes += jsonSize(msg);
    metrics.upstreamRequestTokens += tokenCounter.count(msg);
  });

  createLineReader(upstream.stdout, (line) => {
    const msg = parseMessageLine(line);
    if (!msg) {
      if (traceRpc) {
        process.stderr.write('[ultra-lean-mcp-proxy] rpc<- upstream non-json-line\n');
      }
      process.stdout.write(`${line}\n`);
      return;
    }

    metrics.upstreamResponses += 1;
    metrics.upstreamResponseBytes += jsonSize(msg);
    metrics.upstreamResponseTokens += tokenCounter.count(msg);

    traceUpstream(traceRpc, msg, pending);

    const reqId = msg.id;
    if (reqId !== undefined && Object.prototype.hasOwnProperty.call(msg, 'result')) {
      const pendingReq = pending.get(reqId);
      pending.delete(reqId);

      if (pendingReq && pendingReq.method === 'initialize') {
        if (cfg.toolsHashSyncEnabled && pendingReq.clientToolsHashSyncSupported) {
          toolsHashSyncNegotiated = true;
          try {
            msg.result = injectInitializeToolsHashCapability(msg.result, cfg.toolsHashSyncAlgorithm);
          } catch {
            // fail-open
          }
        } else {
          toolsHashSyncNegotiated = false;
        }
      } else if (pendingReq && pendingReq.method === 'tools/list') {
        try {
          msg.result = handleToolsListResult(
            msg.result,
            state,
            cfg,
            metrics,
            tokenCounter,
            {
              toolsHashSyncNegotiated,
              profileFingerprint,
              ifNoneMatch: pendingReq.toolsHashIfNoneMatch,
              ifNoneMatchProvided: pendingReq.toolsHashIfNoneMatchProvided,
              ifNoneMatchValid: pendingReq.toolsHashIfNoneMatchValid,
            }
          );
        } catch {
          // fail-open
        }
      } else if (pendingReq && pendingReq.method === 'tools/call') {
        try {
          const rawUpstreamResult = cloneJson(msg.result);
          let result = msg.result;
          result = applyResultCompression(
            result,
            pendingReq.toolName,
            cfg,
            metrics,
            tokenCounter,
            featureStates,
            keyRegistry,
            keyRegistryCounter
          );

          if (
            cfg.cachingEnabled
            && !cfg.cacheMutatingTools
            && pendingReq.toolName
            && isMutatingToolName(pendingReq.toolName)
          ) {
            const scopePrefix = `${cfg.sessionId}:${cfg.serverName}:`;
            state.cacheInvalidatePrefix(scopePrefix);
            state.historyInvalidatePrefix(`cache_raw:${scopePrefix}`);
          }

          const cacheKey = pendingReq.cacheKey;
          if (cacheKey && toolCacheAllowed(cfg, pendingReq.toolName)) {
            const baseTtl = cacheTtlForTool(cfg, pendingReq.toolName);
            let ttl = baseTtl;
            if (cfg.cacheAdaptiveTtl && baseTtl > 0) {
              const rawKey = `cache_raw:${cacheKey}`;
              const previousRaw = state.historyGet(rawKey);
              if (previousRaw !== null) {
                const changed = JSON.stringify(previousRaw) !== JSON.stringify(rawUpstreamResult);
                if (changed) {
                  ttl = Math.max(cfg.cacheTtlMinSeconds, Math.floor(baseTtl * 0.5));
                } else {
                  ttl = Math.min(cfg.cacheTtlMaxSeconds, Math.floor(baseTtl * 1.5));
                }
              }
              ttl = Math.min(Math.max(ttl, cfg.cacheTtlMinSeconds), cfg.cacheTtlMaxSeconds);
              state.historySet(rawKey, rawUpstreamResult);
            }
            state.cacheSet(cacheKey, result, ttl);
          }

          const historyKey = cacheKey || makeCacheKey(
            cfg.sessionId,
            cfg.serverName,
            pendingReq.toolName || '_unknown',
            pendingReq.argumentsValue || {}
          );
          result = applyDeltaResponse(
            result,
            historyKey,
            pendingReq.toolName,
            state,
            cfg,
            metrics,
            deltaCounters,
            tokenCounter
          );
          msg.result = result;
        } catch {
          // fail-open
        }
      }
    } else if (reqId !== undefined && Object.prototype.hasOwnProperty.call(msg, 'error')) {
      const pendingReq = pending.get(reqId);
      pending.delete(reqId);
      if (pendingReq && pendingReq.method === 'initialize') {
        toolsHashSyncNegotiated = false;
      }
    }

    sendToClient(msg);
  });

  upstream.stderr.on('data', (chunk) => {
    process.stderr.write(chunk);
  });

  function shutdown(code) {
    if (cfg.stats) {
      process.stderr.write(
        `[ultra-lean-mcp-proxy] stats: tools/list=${metrics.toolsListRequests} saved=${metrics.toolsListSavedBytes}B `
        + `tools_hash_sync hit=${metrics.toolsHashSyncHits} miss=${metrics.toolsHashSyncMisses} `
        + `not_modified=${metrics.toolsHashSyncNotModified} saved=${metrics.toolsHashSyncSavedBytes}B/`
        + `${metrics.toolsHashSyncSavedTokens}tok cache hit=${metrics.cacheHits} miss=${metrics.cacheMisses} `
        + `result_compression=${metrics.resultCompressions} saved=${metrics.resultSavedBytes}B `
        + `delta=${metrics.deltaResponses} saved=${metrics.deltaSavedBytes}B `
        + `search_calls=${metrics.searchCalls} upstream req=${metrics.upstreamRequests}/`
        + `${metrics.upstreamRequestTokens}tok/${metrics.upstreamRequestBytes}B rsp=${metrics.upstreamResponses}/`
        + `${metrics.upstreamResponseTokens}tok/${metrics.upstreamResponseBytes}B\n`
      );
    }
    process.exit(code ?? 0);
  }

  process.stdin.on('end', () => {
    try {
      upstream.stdin.end();
    } catch {
      // ignore
    }
    setTimeout(() => {
      try {
        upstream.kill('SIGTERM');
      } catch {
        // ignore
      }
    }, 2000);
  });

  upstream.on('exit', (code) => {
    shutdown(code);
  });

  upstream.on('error', (err) => {
    process.stderr.write(`[ultra-lean-mcp-proxy] upstream error: ${err.message}\n`);
    process.exit(1);
  });

  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      try {
        upstream.kill(sig);
      } catch {
        // ignore
      }
      setTimeout(() => process.exit(1), 3000);
    });
  }

  return new Promise(() => {});
}
