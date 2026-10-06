#!/usr/bin/env node
// Offline verification of the collected Tauri v2 updater assets.
// Format sources: minisign-verify 0.2.5, src/lib.rs (verify_ed25519/verify_stream),
// and tauri-cli v2.12.1, src/helpers/updater_signature.rs (sign_file/pub_key).
// ED signs BLAKE2b-512(file) with ordinary Ed25519, NOT Ed25519ph.
import { createHash, createPublicKey, verify } from 'node:crypto';
import { lstat, open } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { TextDecoder } from 'node:util';

const EXTENSIONS = Object.freeze({
  'windows-x64': '.exe',
  'linux-x64': '.AppImage',
  'macos-arm64': '.app.tar.gz',
});
const SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');
const MAX_METADATA_BYTES = 16384;

export class VerificationError extends Error {}

function requireCondition(condition, message) {
  if (!condition) throw new VerificationError(message);
}

function base64(value, label) {
  requireCondition(
    typeof value === 'string' && value.length > 0 &&
      value.length <= MAX_METADATA_BYTES &&
      /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value),
    `Malformed ${label}: expected base64`,
  );
  const decoded = Buffer.from(value, 'base64');
  requireCondition(decoded.toString('base64') === value, `Malformed ${label}: noncanonical base64`);
  return decoded;
}

function framedText(value, count, label) {
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(base64(value.trim(), label));
  } catch (error) {
    if (error instanceof VerificationError) throw error;
    throw new VerificationError(`Malformed ${label}: expected UTF-8`);
  }
  const lines = text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n');
  requireCondition(lines.length === count && lines[0].startsWith('untrusted comment: '),
    `Malformed ${label}: invalid minisign framing`);
  return lines;
}

export function parsePublicKey(value) {
  requireCondition(typeof value === 'string' && value.trim().length > 0,
    'Missing WENYI_UPDATER_PUBLIC_KEY');
  const lines = framedText(value, 2, 'updater public key');
  const packet = base64(lines[1], 'public key packet');
  requireCondition(packet.length === 42 && ['Ed', 'ED'].includes(packet.subarray(0, 2).toString()),
    'Malformed updater public key packet');
  return {
    keyId: packet.subarray(2, 10),
    key: createPublicKey({
      key: Buffer.concat([SPKI_PREFIX, packet.subarray(10)]),
      format: 'der',
      type: 'spki',
    }),
  };
}

export function parseSignature(value) {
  const lines = framedText(value, 4, 'updater signature');
  const packet = base64(lines[1], 'signature packet');
  const globalSignature = base64(lines[3], 'global signature');
  requireCondition(packet.length === 74 && packet.subarray(0, 2).toString() === 'ED',
    'Malformed or unsupported updater signature: expected prehashed ED packet');
  requireCondition(globalSignature.length === 64 &&
    lines[2].startsWith('trusted comment: '),
  'Malformed updater signature: invalid trusted metadata');
  return {
    keyId: packet.subarray(2, 10),
    signature: packet.subarray(10),
    trustedComment: lines[2].slice('trusted comment: '.length),
    globalSignature,
  };
}

export function verifyCryptography(digest, publicKey, signature) {
  requireCondition(publicKey.keyId.equals(signature.keyId), 'Updater signature key identity mismatch');
  requireCondition(verify(null, digest, publicKey.key, signature.signature),
    'Updater artifact signature verification failed');
  // The algorithm and key ID are NOT included in the minisign global message.
  const globalMessage = Buffer.concat([
    signature.signature, Buffer.from(signature.trustedComment, 'utf8'),
  ]);
  requireCondition(verify(null, globalMessage, publicKey.key, signature.globalSignature),
    'Updater trusted metadata signature verification failed');
}

export function artifactPath(source, version, platform) {
  requireCondition(typeof version === 'string' &&
    /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version),
  'Expected a stable MAJOR.MINOR.PATCH version');
  requireCondition(Object.hasOwn(EXTENSIONS, platform), 'Unsupported updater platform');
  requireCondition(typeof source === 'string' && source.length > 0, 'Missing updater source directory');
  return resolve(source, `wenyi-desktop-${version}-${platform}${EXTENSIONS[platform]}`);
}

async function openRegularFile(path, label, maximumSize) {
  // Reject symlinks as the existing Python asset collector does.
  try {
    const info = await lstat(path);
    requireCondition(info.isFile() && info.size > 0 &&
      (maximumSize === undefined || info.size <= maximumSize),
    `Missing, empty, or invalid ${label}`);
    return await open(path, 'r');
  } catch (error) {
    if (error instanceof VerificationError) throw error;
    throw new VerificationError(`Cannot read ${label}`);
  }
}

export async function verifyUpdates({ version, platform, source, publicKey }) {
  const path = artifactPath(source, version, platform);
  const key = parsePublicKey(publicKey);
  const signatureFile = await openRegularFile(`${path}.sig`, 'updater signature', MAX_METADATA_BYTES);
  let signature;
  try {
    signature = parseSignature(await signatureFile.readFile({ encoding: 'utf8' }));
  } finally {
    await signatureFile.close();
  }
  const artifact = await openRegularFile(path, 'updater artifact');
  const hash = createHash('blake2b512');
  try {
    for await (const chunk of artifact.createReadStream({ autoClose: false })) hash.update(chunk);
  } catch {
    throw new VerificationError('Cannot read updater artifact');
  } finally {
    await artifact.close();
  }
  verifyCryptography(hash.digest(), key, signature);
  const versions = signature.trustedComment.split('\t').filter(field => field.startsWith('version:'));
  requireCondition(versions.length === 1 && versions[0] === `version:${version}`,
    'Authenticated updater version must equal the expected stable version');
  return { path, platform, version };
}

export function parseArguments(args) {
  const result = {};
  requireCondition(args.length === 6, 'Usage: --version <stable-version> --platform <platform> --source <directory>');
  for (let i = 0; i < args.length; i += 2) {
    const name = args[i];
    requireCondition(['--version', '--platform', '--source'].includes(name) &&
      !Object.hasOwn(result, name.slice(2)) && args[i + 1],
    'Expected unique --version, --platform, and --source arguments');
    result[name.slice(2)] = args[i + 1];
  }
  return result;
}

export async function main(args = process.argv.slice(2), env = process.env) {
  try {
    const options = parseArguments(args);
    const result = await verifyUpdates({ ...options, publicKey: env.WENYI_UPDATER_PUBLIC_KEY });
    console.log(`Verified Desktop updater ${result.platform} version ${result.version}`);
    return 0;
  } catch (error) {
    // Never surface OpenSSL, filesystem, or parser diagnostics containing supplied material.
    console.error(`Desktop updater verification failed: ${error instanceof VerificationError ?
      error.message : 'Unable to verify updater artifact'}`);
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
