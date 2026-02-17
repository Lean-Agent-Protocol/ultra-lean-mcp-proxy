/**
 * Runtime configuration for Ultra Lean MCP Proxy v2 features.
 */

import fs from 'node:fs';
import path from 'node:path';

function parseBool(value, fallback = null) {
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return Boolean(value);
  const text = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'y', 'on'].includes(text)) return true;
  if (['0', 'false', 'no', 'n', 'off'].includes(text)) return false;
  return fallback;
}

function deepMerge(base, override) {
  const out = { ...base };
  for (const [key, value] of Object.entries(override || {})) {
    if (
      value
      && typeof value === 'object'
      && !Array.isArray(value)
      && out[key]
      && typeof out[key] === 'object'
      && !Array.isArray(out[key])
    ) {
      out[key] = deepMerge(out[key], value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

function readConfigFile(configPath) {
  const raw = fs.readFileSync(configPath, 'utf-8');
  const ext = path.extname(configPath).toLowerCase();
  if (ext === '.json' || ext === '') {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Proxy config must be a mapping object');
    }
    return parsed;
  }
  if (ext === '.yaml' || ext === '.yml') {
    throw new Error('YAML config is not supported in npm runtime. Use JSON config.');
  }
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Proxy config must be a mapping object');
  }
  return parsed;
}

function getCliValue(cli, ...keys) {
  for (const key of keys) {
    if (cli[key] !== undefined && cli[key] !== null) {
      return cli[key];
    }
  }
  return undefined;
}

export function createDefaultProxyConfig() {
  return {
    stats: false,
    verbose: false,
    sessionId: 'default',
    strictConfig: false,
    sourcePath: null,

    definitionCompressionEnabled: true,
    definitionMode: 'balanced',

    resultCompressionEnabled: true,
    resultCompressionMode: 'balanced',
    resultMinPayloadBytes: 512,
    resultStripNulls: false,
    resultStripDefaults: false,
    resultMinTokenSavingsAbs: 100,
    resultMinTokenSavingsRatio: 0.05,
    resultMinCompressibility: 0.2,
    resultSharedKeyRegistry: true,
    resultKeyBootstrapInterval: 8,
    resultMinifyRedundantText: true,

    deltaResponsesEnabled: true,
    deltaMinSavingsRatio: 0.15,
    deltaMaxPatchBytes: 65536,
    deltaMaxPatchRatio: 0.8,
    deltaSnapshotInterval: 5,

    lazyLoadingEnabled: true,
    lazyMode: 'minimal',
    lazyTopK: 8,
    lazySemantic: false,
    lazyMinTools: 10,
    lazyMinTokens: 2500,
    lazyMinConfidenceScore: 2,
    lazyFallbackFullOnLowConfidence: true,

    toolsHashSyncEnabled: true,
    toolsHashSyncAlgorithm: 'sha256',
    toolsHashSyncRefreshInterval: 50,
    toolsHashSyncIncludeServerFingerprint: true,

    cachingEnabled: true,
    cacheTtlSeconds: 300,
    cacheMaxEntries: 5000,
    cacheErrors: false,
    cacheMutatingTools: false,
    cacheAdaptiveTtl: true,
    cacheTtlMinSeconds: 30,
    cacheTtlMaxSeconds: 1800,

    autoDisableEnabled: true,
    autoDisableThreshold: 3,
    autoDisableCooldownRequests: 20,

    serverName: 'default',
    toolOverrides: {},
  };
}

function extractServerProfile(configData, upstreamCommand) {
  const servers = configData.servers;
  if (!servers || typeof servers !== 'object' || Array.isArray(servers)) {
    return ['default', {}];
  }
  const commandText = upstreamCommand.join(' ');
  let selectedName = 'default';
  let selectedProfile = {};

  if (servers.default && typeof servers.default === 'object' && !Array.isArray(servers.default)) {
    selectedProfile = { ...servers.default };
  }

  for (const [serverName, profile] of Object.entries(servers)) {
    if (serverName === 'default') continue;
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) continue;
    const match = profile.match;
    if (!match || typeof match !== 'object' || Array.isArray(match)) continue;
    const commandContains = match.command_contains;
    if (typeof commandContains === 'string' && commandText.includes(commandContains)) {
      selectedName = serverName;
      selectedProfile = deepMerge(selectedProfile, profile);
      break;
    }
  }

  return [selectedName, selectedProfile];
}

function applyGlobalConfig(cfg, configData, upstreamCommand, { applyServerProfiles = true } = {}) {
  const out = { ...cfg };
  const proxy = configData.proxy;
  if (proxy && typeof proxy === 'object' && !Array.isArray(proxy)) {
    const stats = parseBool(proxy.stats, null);
    if (stats !== null) out.stats = stats;
    const verbose = parseBool(proxy.verbose, null);
    if (verbose !== null) out.verbose = verbose;
    if (typeof proxy.session_id === 'string' && proxy.session_id) out.sessionId = proxy.session_id;
    if (Number.isInteger(proxy.max_sessions) && proxy.max_sessions > 0) {
      out.cacheMaxEntries = proxy.max_sessions * 10;
    }
    if (typeof proxy.strict_config === 'boolean') out.strictConfig = proxy.strict_config;
  }

  const optimizations = configData.optimizations;
  if (optimizations && typeof optimizations === 'object' && !Array.isArray(optimizations)) {
    const def = optimizations.definition_compression;
    if (def && typeof def === 'object' && !Array.isArray(def)) {
      const enabled = parseBool(def.enabled, null);
      if (enabled !== null) out.definitionCompressionEnabled = enabled;
      if (typeof def.mode === 'string') out.definitionMode = def.mode;
    }

    const rcfg = optimizations.result_compression;
    if (rcfg && typeof rcfg === 'object' && !Array.isArray(rcfg)) {
      const enabled = parseBool(rcfg.enabled, null);
      if (enabled !== null) out.resultCompressionEnabled = enabled;
      if (typeof rcfg.mode === 'string') out.resultCompressionMode = rcfg.mode;
      if (Number.isInteger(rcfg.min_payload_bytes)) out.resultMinPayloadBytes = Math.max(0, rcfg.min_payload_bytes);
      if (Number.isInteger(rcfg.min_token_savings_abs)) {
        out.resultMinTokenSavingsAbs = Math.max(0, rcfg.min_token_savings_abs);
      }
      if (typeof rcfg.min_token_savings_ratio === 'number') {
        out.resultMinTokenSavingsRatio = Math.min(Math.max(rcfg.min_token_savings_ratio, 0), 1);
      }
      if (typeof rcfg.min_compressibility === 'number') {
        out.resultMinCompressibility = Math.min(Math.max(rcfg.min_compressibility, 0), 1);
      }
      const sharedRegistry = parseBool(rcfg.shared_key_registry, null);
      if (sharedRegistry !== null) out.resultSharedKeyRegistry = sharedRegistry;
      if (Number.isInteger(rcfg.key_bootstrap_interval)) {
        out.resultKeyBootstrapInterval = Math.max(0, rcfg.key_bootstrap_interval);
      }
      const minify = parseBool(rcfg.minify_redundant_text, null);
      if (minify !== null) out.resultMinifyRedundantText = minify;
      const stripNulls = parseBool(rcfg.strip_nulls, null);
      if (stripNulls !== null) out.resultStripNulls = stripNulls;
      const stripDefaults = parseBool(rcfg.strip_defaults, null);
      if (stripDefaults !== null) out.resultStripDefaults = stripDefaults;
    }

    const dcfg = optimizations.delta_responses;
    if (dcfg && typeof dcfg === 'object' && !Array.isArray(dcfg)) {
      const enabled = parseBool(dcfg.enabled, null);
      if (enabled !== null) out.deltaResponsesEnabled = enabled;
      if (typeof dcfg.min_savings_ratio === 'number') {
        out.deltaMinSavingsRatio = Math.min(Math.max(dcfg.min_savings_ratio, 0), 1);
      }
      if (Number.isInteger(dcfg.max_patch_bytes)) out.deltaMaxPatchBytes = Math.max(0, dcfg.max_patch_bytes);
      if (typeof dcfg.max_patch_ratio === 'number') {
        out.deltaMaxPatchRatio = Math.min(Math.max(dcfg.max_patch_ratio, 0), 1);
      }
      if (Number.isInteger(dcfg.snapshot_interval)) {
        out.deltaSnapshotInterval = Math.max(1, dcfg.snapshot_interval);
      }
    }

    const lcfg = optimizations.lazy_loading;
    if (lcfg && typeof lcfg === 'object' && !Array.isArray(lcfg)) {
      const enabled = parseBool(lcfg.enabled, null);
      if (enabled !== null) out.lazyLoadingEnabled = enabled;
      if (typeof lcfg.mode === 'string') out.lazyMode = lcfg.mode;
      if (Number.isInteger(lcfg.top_k)) out.lazyTopK = Math.max(1, lcfg.top_k);
      if (Number.isInteger(lcfg.min_tools)) out.lazyMinTools = Math.max(0, lcfg.min_tools);
      if (Number.isInteger(lcfg.min_tokens)) out.lazyMinTokens = Math.max(0, lcfg.min_tokens);
      if (typeof lcfg.min_confidence_score === 'number') out.lazyMinConfidenceScore = lcfg.min_confidence_score;
      const fallback = parseBool(lcfg.fallback_full_on_low_confidence, null);
      if (fallback !== null) out.lazyFallbackFullOnLowConfidence = fallback;
      const semantic = parseBool(lcfg.semantic, null);
      if (semantic !== null) out.lazySemantic = semantic;
    }

    const hcfg = optimizations.tools_hash_sync;
    if (hcfg && typeof hcfg === 'object' && !Array.isArray(hcfg)) {
      const enabled = parseBool(hcfg.enabled, null);
      if (enabled !== null) out.toolsHashSyncEnabled = enabled;
      if (typeof hcfg.algorithm === 'string') out.toolsHashSyncAlgorithm = hcfg.algorithm.trim().toLowerCase();
      if (Number.isInteger(hcfg.refresh_interval)) out.toolsHashSyncRefreshInterval = Math.max(1, hcfg.refresh_interval);
      const includeFingerprint = parseBool(hcfg.include_server_fingerprint, null);
      if (includeFingerprint !== null) out.toolsHashSyncIncludeServerFingerprint = includeFingerprint;
    }

    const ccfg = optimizations.caching;
    if (ccfg && typeof ccfg === 'object' && !Array.isArray(ccfg)) {
      const enabled = parseBool(ccfg.enabled, null);
      if (enabled !== null) out.cachingEnabled = enabled;
      if (Number.isInteger(ccfg.default_ttl_seconds)) out.cacheTtlSeconds = Math.max(0, ccfg.default_ttl_seconds);
      if (Number.isInteger(ccfg.max_entries)) out.cacheMaxEntries = Math.max(1, ccfg.max_entries);
      const cacheErrors = parseBool(ccfg.cache_errors, null);
      if (cacheErrors !== null) out.cacheErrors = cacheErrors;
      const cacheMutating = parseBool(ccfg.cache_mutating_tools, null);
      if (cacheMutating !== null) out.cacheMutatingTools = cacheMutating;
      const adaptive = parseBool(ccfg.adaptive_ttl, null);
      if (adaptive !== null) out.cacheAdaptiveTtl = adaptive;
      if (Number.isInteger(ccfg.ttl_min_seconds)) out.cacheTtlMinSeconds = Math.max(0, ccfg.ttl_min_seconds);
      if (Number.isInteger(ccfg.ttl_max_seconds)) out.cacheTtlMaxSeconds = Math.max(0, ccfg.ttl_max_seconds);
    }

    const acfg = optimizations.auto_disable;
    if (acfg && typeof acfg === 'object' && !Array.isArray(acfg)) {
      const enabled = parseBool(acfg.enabled, null);
      if (enabled !== null) out.autoDisableEnabled = enabled;
      if (Number.isInteger(acfg.threshold)) out.autoDisableThreshold = Math.max(1, acfg.threshold);
      if (Number.isInteger(acfg.cooldown_requests)) {
        out.autoDisableCooldownRequests = Math.max(1, acfg.cooldown_requests);
      }
    }
  }

  if (applyServerProfiles) {
    const [serverName, profile] = extractServerProfile(configData, upstreamCommand);
    out.serverName = serverName;
    if (profile && typeof profile === 'object' && !Array.isArray(profile) && Object.keys(profile).length > 0) {
      const profileOpts = {};
      if (profile.proxy && typeof profile.proxy === 'object') profileOpts.proxy = profile.proxy;
      if (profile.optimizations && typeof profile.optimizations === 'object') {
        profileOpts.optimizations = profile.optimizations;
      }
      if (Object.keys(profileOpts).length > 0) {
        const merged = applyGlobalConfig(out, profileOpts, upstreamCommand, { applyServerProfiles: false });
        Object.assign(out, merged);
      }
      if (profile.tools && typeof profile.tools === 'object' && !Array.isArray(profile.tools)) {
        out.toolOverrides = deepMerge(out.toolOverrides || {}, profile.tools);
      }
    }
  }

  return out;
}

function applyEnv(cfg, env) {
  const out = { ...cfg };

  const stats = parseBool(env.ULTRA_LEAN_MCP_PROXY_STATS, null);
  if (stats !== null) out.stats = stats;
  const verbose = parseBool(env.ULTRA_LEAN_MCP_PROXY_VERBOSE, null);
  if (verbose !== null) out.verbose = verbose;
  if (env.ULTRA_LEAN_MCP_PROXY_SESSION_ID) out.sessionId = String(env.ULTRA_LEAN_MCP_PROXY_SESSION_ID);

  const resultCompression = parseBool(env.ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION, null);
  if (resultCompression !== null) out.resultCompressionEnabled = resultCompression;
  if (env.ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION_MODE) {
    out.resultCompressionMode = String(env.ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION_MODE);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_ABS) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_ABS, 10);
    if (Number.isFinite(n)) out.resultMinTokenSavingsAbs = Math.max(0, n);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_RATIO) {
    const ratio = Number.parseFloat(env.ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_RATIO);
    if (Number.isFinite(ratio)) out.resultMinTokenSavingsRatio = Math.min(Math.max(ratio, 0), 1);
  }
  const sharedRegistry = parseBool(env.ULTRA_LEAN_MCP_PROXY_RESULT_SHARED_KEY_REGISTRY, null);
  if (sharedRegistry !== null) out.resultSharedKeyRegistry = sharedRegistry;
  if (env.ULTRA_LEAN_MCP_PROXY_RESULT_KEY_BOOTSTRAP_INTERVAL) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_RESULT_KEY_BOOTSTRAP_INTERVAL, 10);
    if (Number.isFinite(n)) out.resultKeyBootstrapInterval = Math.max(0, n);
  }
  const minify = parseBool(env.ULTRA_LEAN_MCP_PROXY_RESULT_MINIFY_REDUNDANT_TEXT, null);
  if (minify !== null) out.resultMinifyRedundantText = minify;

  const deltaResponses = parseBool(env.ULTRA_LEAN_MCP_PROXY_DELTA_RESPONSES, null);
  if (deltaResponses !== null) out.deltaResponsesEnabled = deltaResponses;
  if (env.ULTRA_LEAN_MCP_PROXY_DELTA_MIN_SAVINGS) {
    const ratio = Number.parseFloat(env.ULTRA_LEAN_MCP_PROXY_DELTA_MIN_SAVINGS);
    if (Number.isFinite(ratio)) out.deltaMinSavingsRatio = Math.min(Math.max(ratio, 0), 1);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_DELTA_MAX_PATCH_RATIO) {
    const ratio = Number.parseFloat(env.ULTRA_LEAN_MCP_PROXY_DELTA_MAX_PATCH_RATIO);
    if (Number.isFinite(ratio)) out.deltaMaxPatchRatio = Math.min(Math.max(ratio, 0), 1);
  }

  const lazyLoading = parseBool(env.ULTRA_LEAN_MCP_PROXY_LAZY_LOADING, null);
  if (lazyLoading !== null) out.lazyLoadingEnabled = lazyLoading;
  if (env.ULTRA_LEAN_MCP_PROXY_LAZY_MODE) out.lazyMode = String(env.ULTRA_LEAN_MCP_PROXY_LAZY_MODE);
  if (env.ULTRA_LEAN_MCP_PROXY_SEARCH_TOP_K) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_SEARCH_TOP_K, 10);
    if (Number.isFinite(n)) out.lazyTopK = Math.max(1, n);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOOLS) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOOLS, 10);
    if (Number.isFinite(n)) out.lazyMinTools = Math.max(0, n);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOKENS) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOKENS, 10);
    if (Number.isFinite(n)) out.lazyMinTokens = Math.max(0, n);
  }
  if (env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_CONFIDENCE) {
    const n = Number.parseFloat(env.ULTRA_LEAN_MCP_PROXY_LAZY_MIN_CONFIDENCE);
    if (Number.isFinite(n)) out.lazyMinConfidenceScore = n;
  }

  const toolsHashSync = parseBool(env.ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_SYNC, null);
  if (toolsHashSync !== null) out.toolsHashSyncEnabled = toolsHashSync;
  if (env.ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_REFRESH_INTERVAL) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_REFRESH_INTERVAL, 10);
    if (Number.isFinite(n)) out.toolsHashSyncRefreshInterval = Math.max(1, n);
  }

  const caching = parseBool(env.ULTRA_LEAN_MCP_PROXY_CACHING, null);
  if (caching !== null) out.cachingEnabled = caching;
  if (env.ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS) {
    const n = Number.parseInt(env.ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS, 10);
    if (Number.isFinite(n)) out.cacheTtlSeconds = Math.max(0, n);
  }
  const adaptive = parseBool(env.ULTRA_LEAN_MCP_PROXY_CACHE_ADAPTIVE_TTL, null);
  if (adaptive !== null) out.cacheAdaptiveTtl = adaptive;

  return out;
}

function applyCliOverrides(cfg, cli) {
  const out = { ...cfg };
  const setBool = (value, key) => {
    if (value !== undefined && value !== null) {
      out[key] = Boolean(value);
    }
  };

  setBool(getCliValue(cli, 'stats'), 'stats');
  setBool(getCliValue(cli, 'verbose'), 'verbose');
  setBool(getCliValue(cli, 'resultCompression', 'result_compression'), 'resultCompressionEnabled');
  setBool(getCliValue(cli, 'deltaResponses', 'delta_responses'), 'deltaResponsesEnabled');
  setBool(getCliValue(cli, 'lazyLoading', 'lazy_loading'), 'lazyLoadingEnabled');
  setBool(getCliValue(cli, 'toolsHashSync', 'tools_hash_sync'), 'toolsHashSyncEnabled');
  setBool(getCliValue(cli, 'caching'), 'cachingEnabled');

  const sessionId = getCliValue(cli, 'sessionId', 'session_id');
  if (sessionId) out.sessionId = String(sessionId);

  const strictConfig = getCliValue(cli, 'strictConfig', 'strict_config');
  if (strictConfig !== undefined && strictConfig !== null) {
    out.strictConfig = Boolean(strictConfig);
  }

  const cacheTtl = getCliValue(cli, 'cacheTtl', 'cache_ttl');
  if (cacheTtl !== undefined && cacheTtl !== null) {
    out.cacheTtlSeconds = Math.max(0, Number.parseInt(cacheTtl, 10) || 0);
  }
  const deltaMinSavings = getCliValue(cli, 'deltaMinSavings', 'delta_min_savings');
  if (deltaMinSavings !== undefined && deltaMinSavings !== null) {
    const ratio = Number.parseFloat(deltaMinSavings);
    if (Number.isFinite(ratio)) out.deltaMinSavingsRatio = Math.min(Math.max(ratio, 0), 1);
  }
  const lazyMode = getCliValue(cli, 'lazyMode', 'lazy_mode');
  if (lazyMode) out.lazyMode = String(lazyMode);
  const searchTopK = getCliValue(cli, 'searchTopK', 'search_top_k');
  if (searchTopK !== undefined && searchTopK !== null) {
    out.lazyTopK = Math.max(1, Number.parseInt(searchTopK, 10) || 1);
  }
  const resultCompressionMode = getCliValue(cli, 'resultCompressionMode', 'result_compression_mode');
  if (resultCompressionMode) out.resultCompressionMode = String(resultCompressionMode);
  const toolsHashRefreshInterval = getCliValue(
    cli,
    'toolsHashRefreshInterval',
    'tools_hash_refresh_interval'
  );
  if (toolsHashRefreshInterval !== undefined && toolsHashRefreshInterval !== null) {
    out.toolsHashSyncRefreshInterval = Math.max(1, Number.parseInt(toolsHashRefreshInterval, 10) || 1);
  }

  return out;
}

export function featureEnabledForTool(cfg, toolName, featureName, defaultValue) {
  if (!toolName) return defaultValue;
  const toolCfg = cfg.toolOverrides?.[toolName] || {};
  const featureCfg = toolCfg[featureName];
  if (typeof featureCfg === 'boolean') return featureCfg;
  if (featureCfg && typeof featureCfg === 'object' && !Array.isArray(featureCfg)) {
    const enabled = parseBool(featureCfg.enabled, null);
    if (enabled !== null) return enabled;
  }
  return defaultValue;
}

export function cacheTtlForTool(cfg, toolName) {
  if (!toolName) return cfg.cacheTtlSeconds;
  const toolCfg = cfg.toolOverrides?.[toolName] || {};
  const cachingCfg = toolCfg.caching;
  if (cachingCfg && typeof cachingCfg === 'object' && !Array.isArray(cachingCfg)) {
    if (Number.isInteger(cachingCfg.ttl_seconds) && cachingCfg.ttl_seconds >= 0) {
      return cachingCfg.ttl_seconds;
    }
  }
  return cfg.cacheTtlSeconds;
}

export function loadProxyConfig({
  upstreamCommand,
  configPath = null,
  cliOverrides = {},
  env = process.env,
} = {}) {
  if (!Array.isArray(upstreamCommand)) {
    throw new Error('upstreamCommand is required');
  }

  let cfg = createDefaultProxyConfig();
  const resolvedPath = configPath || cliOverrides.configPath || env.ULTRA_LEAN_MCP_PROXY_CONFIG || null;
  if (resolvedPath) {
    const configData = readConfigFile(resolvedPath);
    cfg = applyGlobalConfig(cfg, configData, upstreamCommand);
    cfg.sourcePath = resolvedPath;
  }

  cfg = applyEnv(cfg, env);
  cfg = applyCliOverrides(cfg, cliOverrides);

  if (!['off', 'minimal', 'catalog', 'search_only'].includes(cfg.lazyMode)) {
    throw new Error(`Invalid lazy mode: ${cfg.lazyMode}`);
  }
  if (!['off', 'balanced', 'aggressive'].includes(cfg.resultCompressionMode)) {
    throw new Error(`Invalid result compression mode: ${cfg.resultCompressionMode}`);
  }
  if (cfg.toolsHashSyncAlgorithm !== 'sha256') {
    throw new Error(`Invalid tools hash sync algorithm: ${cfg.toolsHashSyncAlgorithm}`);
  }
  if (cfg.cacheTtlMaxSeconds < cfg.cacheTtlMinSeconds) {
    cfg.cacheTtlMaxSeconds = cfg.cacheTtlMinSeconds;
  }
  if (cfg.lazyMode !== 'off') {
    cfg.lazyLoadingEnabled = true;
  }
  if (cfg.resultCompressionMode === 'off') {
    cfg.resultCompressionEnabled = false;
  }
  return cfg;
}
