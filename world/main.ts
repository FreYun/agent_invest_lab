#!/usr/bin/env -S node --experimental-strip-types
import { main } from './src/cli.ts'
main().then(code => { process.exitCode = code }).catch(err => { process.stderr.write((err instanceof Error ? err.stack || err.message : String(err)) + '\n'); process.exitCode = 1 })
