#!/usr/bin/env node

/**
 * Ultra Lean MCP Proxy - CLI entry point.
 *
 * Commands:
 *   install   [--dry-run] [--client NAME] [--skip SERVER] [--offline] [--runtime npm|pip] [--no-wrap-url] [-v]
 *   uninstall [--dry-run] [--client NAME] [--runtime npm|pip] [--all] [-v]
 *   status
 *   wrap-cloud [--dry-run] [--runtime npm|pip] [--suffix NAME_SUFFIX] [-v]
 *   proxy     [v2 flags] [--stats] [--trace-rpc] [--runtime npm] -- <upstream-command>
 *   watch     [--interval SEC] [--daemon] [--stop] [--offline] [--no-wrap-url] [-v]
 */

import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Helper: dynamic import by file path (works cross-platform with ESM)
function importLocal(relPath) {
  const absPath = join(__dirname, '..', relPath);
  return import(pathToFileURL(absPath).href);
}

// ---------------------------------------------------------------------------
// Minimal argument parser -- no external deps
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = argv.slice(0);
  const result = {
    command: null,
    flags: {},
    positional: [],
    rest: [],       // everything after "--"
  };

  function setFlagValue(key, value) {
    if (!(key in result.flags)) {
      result.flags[key] = value;
      return;
    }
    if (Array.isArray(result.flags[key])) {
      result.flags[key].push(value);
      return;
    }
    result.flags[key] = [result.flags[key], value];
  }

  // Extract the subcommand (first non-flag token)
  let i = 0;
  while (i < args.length) {
    if (args[i] === '--') {
      result.rest = args.slice(i + 1);
      break;
    }
    if (args[i].startsWith('-')) {
      // flag
      const raw = args[i].replace(/^-+/, '');
      // Check for --flag=value
      if (args[i].includes('=')) {
        const [key, ...valParts] = raw.split('=');
        setFlagValue(key, valParts.join('='));
      } else if (
        i + 1 < args.length &&
        !args[i + 1].startsWith('-') &&
        args[i + 1] !== '--'
      ) {
        // Peek: boolean-like short flags that never take a value
        const boolFlags = new Set([
          'v', 'verbose', 'dry-run', 'stats', 'offline', 'all', 'help', 'h',
          'daemon', 'stop', 'trace-rpc', 'no-wrap-url', 'include-url',
          'strict-config', 'dump-effective-config',
          'no-cloud',
          'enable-result-compression', 'disable-result-compression',
          'enable-delta-responses', 'disable-delta-responses',
          'enable-lazy-loading', 'disable-lazy-loading',
          'enable-tools-hash-sync', 'disable-tools-hash-sync',
          'enable-caching', 'disable-caching',
        ]);
        if (boolFlags.has(raw)) {
          result.flags[raw] = true;
        } else {
          setFlagValue(raw, args[i + 1]);
          i++;
        }
      } else {
        result.flags[raw] = true;
      }
    } else if (result.command === null) {
      result.command = args[i];
    } else {
      result.positional.push(args[i]);
    }
    i++;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Help text
// ---------------------------------------------------------------------------

const HELP = `
ultra-lean-mcp-proxy - lightweight optimization proxy for MCP

Usage:
  ultra-lean-mcp-proxy install   [--dry-run] [--client NAME] [--skip SERVER] [--offline] [--runtime npm|pip] [--no-wrap-url] [--no-cloud] [--suffix NAME] [-v]
  ultra-lean-mcp-proxy uninstall [--dry-run] [--client NAME] [--runtime npm|pip] [--all] [-v]
  ultra-lean-mcp-proxy status
  ultra-lean-mcp-proxy wrap-cloud [--dry-run] [--runtime npm|pip] [--suffix NAME_SUFFIX] [-v]
  ultra-lean-mcp-proxy proxy     [v2 flags] [--stats] [--trace-rpc] [--runtime npm] -- <upstream-command>
  ultra-lean-mcp-proxy watch     [--interval SEC] [--daemon] [--stop] [--offline] [--no-wrap-url] [--suffix NAME] [--cloud-interval SEC] [-v]

Options:
  -v, --verbose    Verbose output
  -h, --help       Show this help message

Commands:
  install     Wrap MCP server entries in client configs to route through proxy
  uninstall   Remove proxy wrapping from client configs
  status      Show which clients/servers are currently wrapped
  wrap-cloud  Mirror cloud-scoped Claude MCP URL connectors into local config, already wrapped
  proxy       Run as stdio proxy in front of an upstream MCP server
    --enable-result-compression / --disable-result-compression
    --enable-delta-responses / --disable-delta-responses
    --enable-lazy-loading / --disable-lazy-loading
    --enable-tools-hash-sync / --disable-tools-hash-sync
    --enable-caching / --disable-caching
    --cache-ttl <seconds> --delta-min-savings <ratio> --lazy-mode off|minimal|search_only
  watch       Watch config files and auto-wrap new MCP servers
    --daemon     Run watcher as background daemon
    --stop       Stop running daemon
    --interval   Polling interval in seconds (default: 5)
    --suffix     Suffix for cloud connector names (default: -ulmp)
    --cloud-interval  Cloud discovery interval in seconds (default: 60)
`.trim();

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const parsed = parseArgs(process.argv.slice(2));
  const firstValue = (value, fallback = null) => (Array.isArray(value) ? value[value.length - 1] : (value ?? fallback));

  if (parsed.flags.help || parsed.flags.h || !parsed.command) {
    console.log(HELP);
    process.exit(parsed.command ? 0 : 1);
  }

  const verbose = !!(parsed.flags.v || parsed.flags.verbose);

  switch (parsed.command) {
    // ---- install ----
    case 'install': {
      const { doInstall } = await importLocal('src/installer.mjs');
      const skipRaw = parsed.flags.skip;
      const wrapUrl = parsed.flags['no-wrap-url'] ? false : true;
      const installRuntime = firstValue(parsed.flags.runtime, 'npm');
      if (!['npm', 'pip'].includes(String(installRuntime))) {
        console.error(`Error: invalid --runtime value "${installRuntime}". Expected "npm" or "pip".`);
        process.exit(1);
      }
      await doInstall({
        dryRun: !!parsed.flags['dry-run'],
        clientFilter: firstValue(parsed.flags.client, null),
        skipNames: Array.isArray(skipRaw) ? skipRaw : skipRaw ? [skipRaw] : [],
        offline: !!parsed.flags.offline,
        wrapUrl,
        runtime: installRuntime,
        verbose,
      });

      // Cloud connector discovery (enabled by default, opt-out with --no-cloud)
      if (!parsed.flags['no-cloud']) {
        const { commandExists, doWrapCloud } = await importLocal('src/installer.mjs');
        if (commandExists('claude')) {
          try {
            const suffix = firstValue(parsed.flags.suffix, '-ulmp');
            await doWrapCloud({
              dryRun: !!parsed.flags['dry-run'],
              runtime: installRuntime,
              suffix,
              verbose,
            });
          } catch (err) {
            console.log(`[install] Cloud connector discovery failed: ${err.message}`);
          }
        } else {
          console.log('[install] Cloud connector discovery skipped: claude CLI not found on PATH');
        }
      }
      break;
    }

    // ---- uninstall ----
    case 'uninstall': {
      const { doUninstall } = await importLocal('src/installer.mjs');
      await doUninstall({
        dryRun: !!parsed.flags['dry-run'],
        clientFilter: firstValue(parsed.flags.client, null),
        runtime: firstValue(parsed.flags.runtime, 'npm'),
        all: !!parsed.flags.all,
        verbose,
      });
      break;
    }

    // ---- status ----
    case 'status': {
      const { showStatus } = await importLocal('src/installer.mjs');
      await showStatus();
      break;
    }

    // ---- wrap-cloud ----
    case 'wrap-cloud': {
      const { doWrapCloud } = await importLocal('src/installer.mjs');
      const wrapCloudRuntime = firstValue(parsed.flags.runtime, 'npm');
      if (!['npm', 'pip'].includes(String(wrapCloudRuntime))) {
        console.error(`Error: invalid --runtime value "${wrapCloudRuntime}". Expected "npm" or "pip".`);
        process.exit(1);
      }
      await doWrapCloud({
        dryRun: !!parsed.flags['dry-run'],
        runtime: wrapCloudRuntime,
        suffix: firstValue(parsed.flags.suffix, '-ulmp'),
        verbose,
      });
      break;
    }

    // ---- proxy ----
    case 'proxy': {
      const upstreamCmd = parsed.rest;
      if (upstreamCmd.length === 0) {
        console.error('Error: No upstream server command provided.');
        console.error('Usage: ultra-lean-mcp-proxy proxy -- <command> [args...]');
        console.error('Example: ultra-lean-mcp-proxy proxy -- npx @modelcontextprotocol/server-filesystem /tmp');
        process.exit(1);
      }
      // --runtime is accepted but only used by the wrapper / installer; ignored here.
      const stats = !!parsed.flags.stats;
      const traceRpc = !!parsed.flags['trace-rpc'];
      const toggle = (enableFlag, disableFlag) => {
        if (parsed.flags[enableFlag]) return true;
        if (parsed.flags[disableFlag]) return false;
        return null;
      };

      const { runProxy } = await importLocal('src/proxy.mjs');
      await runProxy(upstreamCmd, {
        stats,
        traceRpc,
        verbose,
        configPath: firstValue(parsed.flags.config, null),
        sessionId: firstValue(parsed.flags['session-id'], null),
        strictConfig: parsed.flags['strict-config'] ? true : null,
        resultCompression: toggle('enable-result-compression', 'disable-result-compression'),
        deltaResponses: toggle('enable-delta-responses', 'disable-delta-responses'),
        lazyLoading: toggle('enable-lazy-loading', 'disable-lazy-loading'),
        toolsHashSync: toggle('enable-tools-hash-sync', 'disable-tools-hash-sync'),
        caching: toggle('enable-caching', 'disable-caching'),
        cacheTtl: firstValue(parsed.flags['cache-ttl'], null),
        deltaMinSavings: firstValue(parsed.flags['delta-min-savings'], null),
        lazyMode: firstValue(parsed.flags['lazy-mode'], null),
        toolsHashRefreshInterval: firstValue(parsed.flags['tools-hash-refresh-interval'], null),
        searchTopK: firstValue(parsed.flags['search-top-k'], null),
        resultCompressionMode: firstValue(parsed.flags['result-compression-mode'], null),
        dumpEffectiveConfig: !!parsed.flags['dump-effective-config'],
      });
      break;
    }

    // ---- watch ----
    case 'watch': {
      const interval = parseInt(firstValue(parsed.flags.interval, 5), 10) || 5;
      const offline = !!parsed.flags.offline;
      const runtime = firstValue(parsed.flags.runtime, 'npm');
      const wrapUrl = parsed.flags['no-wrap-url'] ? false : true;
      const suffix = firstValue(parsed.flags.suffix, '-ulmp');
      const cloudInterval = parseInt(firstValue(parsed.flags['cloud-interval'], 60), 10) || 60;

      if (parsed.flags.stop) {
        const { stopDaemon } = await importLocal('src/watcher.mjs');
        stopDaemon();
      } else if (parsed.flags.daemon) {
        const { startDaemon } = await importLocal('src/watcher.mjs');
        startDaemon({ interval, runtime, offline, wrapUrl, verbose, suffix, cloudInterval });
      } else {
        const { runWatch } = await importLocal('src/watcher.mjs');
        await runWatch({ interval, runtime, offline, wrapUrl, verbose, suffix, cloudInterval });
      }
      break;
    }

    default:
      console.error(`Unknown command: ${parsed.command}`);
      console.log(HELP);
      process.exit(1);
  }
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
