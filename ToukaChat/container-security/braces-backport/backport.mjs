// Fixed public-source backport. No package code, npm, shell or network is executed.
import { createHash, randomBytes } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const HOME = path.dirname(SELF);
const LIMITS = Object.freeze({
  json_bytes: 1048576, file_bytes: 1048576, package_files: 64,
  package_bytes: 262144, lock_nodes: 16384, installed_packages: 16384,
  topology_depth: 64, deadline_ms: 25000,
});
const PATCH_FILES = ['lib/compile.js', 'lib/constants.js', 'lib/expand.js', 'lib/parse.js', 'lib/stringify.js'];
const started = performance.now();
const observed = new Map();
const digest = body => 'sha256:' + createHash('sha256').update(body).digest('hex');
const check = (value, code) => { if (!value) throw new Error(code); };
const tick = () => check(performance.now() - started <= LIMITS.deadline_ms, 'deadline');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const keys = (value, expected) => check(object(value) && Object.keys(value).sort().join('\0') === [...expected].sort().join('\0'), 'schema');
const canonical = value => {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (object(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
};
const jsonBytes = value => Buffer.from(canonical(value) + '\n');
const fingerprint = stat => [stat.dev, stat.ino, stat.mode, stat.nlink, stat.size, stat.mtimeNs, stat.ctimeNs].join(':');

function inspect(target, directory) {
  tick();
  const stat = fs.lstatSync(target, { bigint: true });
  check(!stat.isSymbolicLink() && (directory ? stat.isDirectory() : stat.isFile() && stat.nlink === 1n), 'file-kind');
  const identity = fingerprint(stat);
  const prior = observed.get(target);
  check(!prior || prior.identity === identity, 'identity-drift');
  observed.set(target, { identity, directory, hash: prior?.hash });
  return stat;
}

function directory(target) {
  inspect(target, true);
  const entries = fs.readdirSync(target).sort();
  check(entries.length <= LIMITS.installed_packages, 'directory-bound');
  check(new Set(entries.map(name => name.toLowerCase())).size === entries.length, 'path-alias');
  return entries;
}

function physicalAncestors(target) {
  let cursor = path.resolve(target);
  while (true) {
    // Ancestors are checked for indirection, but unrelated directory activity
    // outside the explicitly owned root is not included in the snapshot.
    const stat = fs.lstatSync(cursor);
    check(stat.isDirectory() && !stat.isSymbolicLink(), 'parent-indirection');
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
  check(path.resolve(fs.realpathSync(target)) === path.resolve(target), 'path-alias');
}

function read(target, maximum = LIMITS.file_bytes) {
  const stat = inspect(target, false);
  check(stat.size <= BigInt(maximum), 'file-bound');
  const fd = fs.openSync(target, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    check(fingerprint(fs.fstatSync(fd, { bigint: true })) === fingerprint(stat), 'open-drift');
    // Bound allocation and reads even if a file grows after the initial stat.
    const buffer = Buffer.alloc(Number(stat.size) + 1);
    let length = 0;
    while (length < buffer.length) {
      tick();
      const received = fs.readSync(fd, buffer, length, buffer.length - length, null);
      if (received === 0) break;
      length += received;
    }
    const body = buffer.subarray(0, length);
    check(body.length <= maximum && BigInt(body.length) === stat.size, 'read-bound');
    check(fingerprint(fs.fstatSync(fd, { bigint: true })) === fingerprint(stat), 'read-drift');
    inspect(target, false);
    const prior = observed.get(target);
    const hash = digest(body);
    check(!prior.hash || prior.hash === hash, 'byte-drift');
    prior.hash = hash;
    return body;
  } finally {
    fs.closeSync(fd);
  }
}

function recheck() {
  for (const [target, prior] of observed) {
    inspect(target, prior.directory);
    if (prior.hash) read(target);
  }
}

function parseJSON(body) {
  check(body.length <= LIMITS.json_bytes, 'json-bound');
  const source = body.toString('utf8');
  check(Buffer.from(source).equals(body), 'json-encoding');
  // JSON.parse validates grammar; this bounded token pass rejects duplicate
  // decoded keys before they could silently overwrite a lock or policy field.
  const result = JSON.parse(source);
  let cursor = 0;
  const whitespace = () => { while (/\s/.test(source[cursor] || '') && cursor < source.length) cursor++; };
  const string = () => {
    const start = cursor++;
    while (cursor < source.length) {
      const value = source[cursor++];
      if (value === '\\') cursor++;
      else if (value === '"') return JSON.parse(source.slice(start, cursor));
    }
    throw new Error('json-string');
  };
  const visit = depth => {
    check(depth <= 128, 'json-depth');
    tick();
    whitespace();
    if (source[cursor] === '{') {
      cursor++;
      whitespace();
      const names = new Set();
      if (source[cursor] !== '}') {
        while (true) {
          whitespace();
          check(source[cursor] === '"', 'json-key');
          const name = string();
          check(!names.has(name), 'duplicate-key');
          names.add(name);
          whitespace();
          check(source[cursor++] === ':', 'json-colon');
          visit(depth + 1);
          whitespace();
          if (source[cursor] !== ',') break;
          cursor++;
        }
      }
      check(source[cursor++] === '}', 'json-object');
    } else if (source[cursor] === '[') {
      cursor++;
      whitespace();
      if (source[cursor] !== ']') {
        while (true) {
          visit(depth + 1);
          whitespace();
          if (source[cursor] !== ',') break;
          cursor++;
        }
      }
      check(source[cursor++] === ']', 'json-array');
    } else if (source[cursor] === '"') string();
    else {
      const start = cursor;
      while (cursor < source.length && !/[\s,}\]]/.test(source[cursor])) cursor++;
      check(cursor > start, 'json-value');
    }
  };
  visit(0);
  whitespace();
  check(cursor === source.length, 'json-trailing');
  return result;
}

function relative(name) {
  check(typeof name === 'string' && name.length > 0 && name.length <= 512 && !/[\\:\0]/.test(name), 'relative-path');
  check(name.split('/').every(part => /^[A-Za-z0-9@_.-]+$/.test(part) && part !== '.' && part !== '..'), 'relative-path');
  return name;
}

function identity(value) {
  keys(value, ['bytes', 'sha256']);
  check(Number.isSafeInteger(value.bytes) && value.bytes >= 0 && value.bytes <= LIMITS.file_bytes && /^sha256:[a-f0-9]{64}$/.test(value.sha256), 'file-identity');
}

function matches(body, expected) {
  return body.length === expected.bytes && digest(body) === expected.sha256;
}

function policyInput() {
  physicalAncestors(HOME);
  const body = read(path.join(HOME, 'manifest.json'));
  const policy = parseJSON(body);
  keys(policy, ['schema', 'package', 'projects', 'upstream', 'patch', 'depth', 'files', 'limits']);
  check(jsonBytes(policy).equals(body) && policy.schema === 'touka.braces-backport.policy.v1', 'policy-canonical');
  check(canonical(policy.limits) === canonical(LIMITS), 'policy-limits');
  check(canonical(policy.projects) === canonical({ ToukaChat: 'frontend', ToukaIDE: 'toukaide' }), 'policy-projects');
  check(canonical(policy.depth) === canonical({ default: 100, maximum: 100 }), 'policy-depth');
  check(canonical(policy.upstream) === canonical({ repository: 'micromatch/braces', pr: 72, base: 'e53730e6f935498326c72d768889ac194eedc0e0', head: '28d440b5dd449dbf1fe6f3506cf94ecca4d02660' }), 'policy-upstream');
  keys(policy.package, ['name', 'version', 'url', 'bytes', 'sha256', 'integrity']);
  check(policy.package.name === 'braces' && policy.package.version === '3.0.3' && policy.package.url === 'https://registry.npmjs.org/braces/-/braces-3.0.3.tgz' && policy.package.bytes === 13972 && policy.package.sha256 === 'sha256:1cd18e862c8640b4568b1425a7df4ee030ff201d45b2da8f9f222d2987494ffc' && policy.package.integrity === 'sha512-yQbXgO/OSZVD2IsiLlro+7Hf6Q18EJrKSEsdoMzKePKXct3gvD8oLcOQdIzGupr5Fj+EDe8gO/lxc1BzfMpxvA==', 'policy-package');
  keys(policy.patch, ['path', 'bytes', 'sha256', 'files']);
  check(policy.patch.path === 'braces-3.0.3-depth-limit.patch' && canonical(policy.patch.files) === canonical(PATCH_FILES), 'policy-patch');
  identity({ bytes: policy.patch.bytes, sha256: policy.patch.sha256 });
  check(Array.isArray(policy.files) && policy.files.length > 0 && policy.files.length <= LIMITS.package_files, 'policy-files');
  const names = [];
  let bytes = 0;
  for (const row of policy.files) {
    keys(row, ['path', 'before', 'after']);
    relative(row.path);
    identity(row.before);
    identity(row.after);
    check((canonical(row.before) !== canonical(row.after)) === PATCH_FILES.includes(row.path), 'policy-change-set');
    names.push(row.path);
    bytes += Math.max(row.before.bytes, row.after.bytes);
  }
  check(bytes <= LIMITS.package_bytes && canonical(names) === canonical([...new Set(names)].sort()) && new Set(names.map(name => name.toLowerCase())).size === names.length, 'policy-inventory');
  check(PATCH_FILES.every(name => names.includes(name)) && ['package.json', 'LICENSE', 'index.js'].every(name => names.includes(name)), 'policy-inventory');
  const patch = read(path.join(HOME, policy.patch.path));
  check(matches(patch, policy.patch), 'patch-identity');
  return { policy, body, patch, helper: read(SELF) };
}

function parsePatch(raw) {
  const text = raw.toString('utf8');
  check(Buffer.from(text).equals(raw) && !text.includes('\r') && text.endsWith('\n'), 'patch-encoding');
  const lines = text.split('\n');
  lines.pop();
  const files = new Map();
  let cursor = 0;
  while (cursor < lines.length) {
    check(lines[cursor].startsWith('--- a/'), 'patch-header');
    const name = relative(lines[cursor++].slice(6));
    check(lines[cursor++] === '+++ b/' + name && !files.has(name), 'patch-header');
    const hunks = [];
    while (cursor < lines.length && lines[cursor].startsWith('@@ ')) {
      const match = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$/.exec(lines[cursor++]);
      check(match, 'patch-hunk');
      const hunk = { old: Number(match[1]) - 1, oldCount: Number(match[2] ?? 1), next: Number(match[3]) - 1, newCount: Number(match[4] ?? 1), lines: [] };
      while (cursor < lines.length && !lines[cursor].startsWith('@@ ') && !lines[cursor].startsWith('--- a/')) {
        check(/^[ +\-]/.test(lines[cursor]), 'patch-line');
        hunk.lines.push(lines[cursor++]);
      }
      check(hunk.lines.filter(line => line[0] !== '+').length === hunk.oldCount && hunk.lines.filter(line => line[0] !== '-').length === hunk.newCount, 'patch-count');
      hunks.push(hunk);
    }
    check(hunks.length > 0, 'patch-empty');
    files.set(name, hunks);
  }
  check(canonical([...files.keys()]) === canonical(PATCH_FILES), 'patch-files');
  return files;
}

function patched(raw, hunks) {
  const source = raw.toString('utf8');
  check(Buffer.from(source).equals(raw) && source.endsWith('\n') && !source.includes('\r'), 'source-encoding');
  const lines = source.split('\n');
  lines.pop();
  const output = [];
  let cursor = 0;
  for (const hunk of hunks) {
    check(hunk.old >= cursor && hunk.old <= lines.length, 'patch-position');
    output.push(...lines.slice(cursor, hunk.old));
    cursor = hunk.old;
    check(output.length === hunk.next, 'patch-position');
    for (const line of hunk.lines) {
      if (line[0] !== '+') check(lines[cursor++] === line.slice(1), 'patch-context');
      if (line[0] !== '-') output.push(line.slice(1));
    }
  }
  output.push(...lines.slice(cursor));
  return Buffer.from(output.join('\n') + '\n');
}

function lockNodes(lock, policy) {
  check(object(lock) && lock.lockfileVersion === 3 && object(lock.packages) && object(lock.packages['']), 'lock-schema');
  const all = Object.entries(lock.packages);
  check(all.length <= LIMITS.lock_nodes, 'lock-bound');
  const seen = new Set();
  const nodes = new Map();
  for (const [name, row] of all) {
    if (name) relative(name);
    check(!seen.has(name.toLowerCase()) && object(row), 'lock-path-alias');
    seen.add(name.toLowerCase());
    const braces = name === 'node_modules/braces' || name.endsWith('/node_modules/braces');
    check(row.name !== 'braces' || braces, 'lock-alias');
    if (!braces) continue;
    check(row.version === policy.package.version && row.resolved === policy.package.url && row.integrity === policy.package.integrity && !row.link, 'lock-braces');
    nodes.set(name, { path: name, parents: [], files: [], dev: row.dev });
  }
  check(nodes.size > 0, 'lock-braces-missing');
  for (const [parent, row] of all) {
    for (const field of ['dependencies', 'optionalDependencies']) {
      if (row[field] === undefined) continue;
      check(object(row[field]), 'lock-dependencies');
      for (const [dependency, version] of Object.entries(row[field])) {
        relative(dependency);
        check(typeof version === 'string' && !(dependency !== 'braces' && /^npm:braces(?:@|$)/.test(version)), 'lock-dependency-alias');
        if (dependency !== 'braces') continue;
        let cursor = parent;
        let resolved;
        while (true) {
          if (path.posix.basename(cursor) !== 'node_modules') {
            const candidate = (cursor ? cursor + '/' : '') + 'node_modules/' + dependency;
            if (Object.hasOwn(lock.packages, candidate)) { resolved = candidate; break; }
          }
          if (!cursor) break;
          cursor = path.posix.dirname(cursor);
          if (cursor === '.') cursor = '';
        }
        check(nodes.has(resolved), 'lock-parent-resolution');
        nodes.get(resolved).parents.push(parent);
      }
    }
  }
  for (const row of nodes.values()) {
    row.parents = [...new Set(row.parents)].sort();
    check(row.parents.length > 0, 'lock-orphan');
  }
  return [...nodes.values()].sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
}

function topology(root) {
  const braces = new Map();
  let count = 0;
  const present = target => {
    // existsSync follows links, which would misclassify a dangling link as
    // absence. Only an actual missing directory entry can mean absence.
    try { fs.lstatSync(target); return true; }
    catch (error) { if (error.code === 'ENOENT') return false; throw error; }
  };
  const scanCache = (target, depth) => {
    check(depth <= LIMITS.topology_depth, 'cache-depth');
    for (const name of directory(target)) {
      check(++count <= LIMITS.installed_packages, 'topology-bound');
      relative(name);
      check(!['node_modules', 'braces'].includes(name.toLowerCase()), 'cache-package');
      const child = path.join(target, name);
      const stat = fs.lstatSync(child);
      if (stat.isDirectory() && !stat.isSymbolicLink()) scanCache(child, depth + 1);
      else {
        // Generated chunks may be larger than a package file. Their regular
        // metadata is checked, but their contents are never read or attested.
        inspect(child, false);
        if (name.toLowerCase() === 'package.json') {
          check(canonical(parseJSON(read(child))) === canonical({ type: 'module' }), 'cache-descriptor');
        }
      }
    }
  };
  const scanPackage = (target, relativePath, depth) => {
    check(++count <= LIMITS.installed_packages && depth <= LIMITS.topology_depth, 'topology-bound');
    inspect(target, true);
    const doc = parseJSON(read(path.join(target, 'package.json')));
    check(object(doc) && typeof doc.name === 'string', 'installed-package');
    const named = path.basename(target) === 'braces';
    check((doc.name === 'braces') === named, 'installed-alias');
    if (named) braces.set(relativePath, target);
    const nested = path.join(target, 'node_modules');
    if (present(nested)) scanModules(nested, relativePath + '/node_modules', depth + 1);
  };
  const scanModules = (target, relativePath, depth) => {
    check(depth <= LIMITS.topology_depth, 'topology-depth');
    for (const name of directory(target)) {
      if (name === '.bin') continue; // Never follow package manager executable links.
      if (name === '.package-lock.json') { read(path.join(target, name)); continue; }
      relative(name);
      if (name === '.vite' || name === '.vite-temp') {
        scanCache(path.join(target, name), depth + 1);
        continue;
      }
      check(!name.startsWith('.'), 'unknown-metadata');
      if (name.startsWith('@')) {
        for (const member of directory(path.join(target, name))) {
          relative(member);
          scanPackage(path.join(target, name, member), relativePath + '/' + name + '/' + member, depth + 1);
        }
      } else scanPackage(path.join(target, name), relativePath + '/' + name, depth + 1);
    }
  };
  const modules = path.join(root, 'node_modules');
  if (present(modules)) scanModules(modules, 'node_modules', 0);
  return braces;
}

function inventory(target, policy) {
  const found = new Map();
  const allowedDirectories = new Set();
  for (const row of policy.files) {
    const parts = row.path.split('/');
    for (let index = 1; index < parts.length; index++) allowedDirectories.add(parts.slice(0, index).join('/'));
  }
  let total = 0;
  const walk = (folder, prefix) => {
    for (const name of directory(folder)) {
      relative(name);
      const key = prefix ? prefix + '/' + name : name;
      const filename = path.join(folder, name);
      const stat = fs.lstatSync(filename);
      if (stat.isDirectory() && !stat.isSymbolicLink()) {
        check(allowedDirectories.has(key), 'package-directory');
        walk(filename, key);
      } else {
        check(found.size < LIMITS.package_files, 'package-file-bound');
        const body = read(filename);
        total += body.length;
        check(total <= LIMITS.package_bytes, 'package-byte-bound');
        found.set(key, body);
      }
    }
  };
  walk(target, '');
  check(canonical([...found.keys()].sort()) === canonical(policy.files.map(row => row.path)), 'package-inventory');
  return found;
}

function replace(target, body) {
  physicalAncestors(path.dirname(target));
  const before = inspect(target, false);
  // No overwrite of an existing temporary file. A failed write is preserved;
  // a later invocation rejects partial package bytes instead of resuming them.
  const temporary = target + '.touka-' + randomBytes(12).toString('hex') + '.tmp';
  const fd = fs.openSync(temporary, 'wx', Number(before.mode & 0o777n));
  try { fs.writeFileSync(fd, body); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  inspect(target, false);
  fs.renameSync(temporary, target);
}

function main() {
  const args = process.argv.slice(2);
  const action = args[0];
  check((action === 'apply' && args.length === 3) || (action === 'verify' && args.length === 5 && args[3] === '--scope' && ['full', 'runtime'].includes(args[4])), 'arguments');
  check(args[1] === '--project-root' && typeof args[2] === 'string' && args[2].length > 0, 'arguments');
  const scope = action === 'apply' ? 'full' : args[4];
  const root = path.resolve(args[2]);
  physicalAncestors(root);
  inspect(root, true);
  const input = policyInput();
  const patch = parsePatch(input.patch);
  const packageBody = read(path.join(root, 'package.json'));
  const lockBody = read(path.join(root, 'package-lock.json'));
  const pkg = parseJSON(packageBody);
  const lock = parseJSON(lockBody);
  const project = Object.keys(input.policy.projects).find(key => input.policy.projects[key] === pkg.name);
  check(project && lock.name === pkg.name && lock.packages?.['']?.name === pkg.name, 'project');
  const nodes = lockNodes(lock, input.policy);
  const actual = topology(root);
  const pending = [];
  const proofBase = {
    schema: 'touka.braces-backport.installed-bytes.v1', project, scope,
    package_sha256: digest(packageBody), lock_sha256: digest(lockBody),
    policy_sha256: digest(input.body), helper_sha256: digest(input.helper), patch_sha256: digest(input.patch),
  };
  let proofSize = jsonBytes({ ...proofBase, nodes: [] }).length;
  let accounted = 0;
  const account = node => {
    proofSize += Buffer.byteLength(canonical({ path: node.path, parents: node.parents, files: node.files })) + (accounted++ ? 1 : 0);
    check(proofSize <= LIMITS.json_bytes, 'stdout-bound');
  };
  if (scope === 'runtime') {
    check(nodes.every(node => node.dev === true) && actual.size === 0, 'runtime-present');
    nodes.forEach(account);
  } else {
    check(canonical([...actual.keys()].sort()) === canonical(nodes.map(node => node.path)), 'installed-node-set');
    const states = new Set();
    for (const node of nodes) {
      const files = inventory(actual.get(node.path), input.policy);
      const original = input.policy.files.every(row => matches(files.get(row.path), row.before));
      const ready = input.policy.files.every(row => matches(files.get(row.path), row.after));
      check(original || ready, 'unrecognized-package-bytes');
      states.add(original ? 'original' : 'patched');
      check(action === 'apply' || ready, 'not-patched');
      for (const row of input.policy.files) {
        let body = files.get(row.path);
        if (original && patch.has(row.path)) {
          body = patched(body, patch.get(row.path));
          check(matches(body, row.after), 'patched-identity');
          pending.push({ target: path.join(actual.get(node.path), row.path), body });
        }
        node.files.push({ path: row.path, bytes: body.length, sha256: digest(body), body_base64: body.toString('base64') });
      }
      account(node);
    }
    check(states.size === 1, 'mixed-installation');
  }
  const proof = jsonBytes({ ...proofBase, nodes: nodes.map(({ path: nodePath, parents, files }) => ({ path: nodePath, parents, files })) });
  check(proof.length === proofSize && proof.length <= LIMITS.json_bytes, 'stdout-bound');
  // Validate every file/topology before the first write, including all copies.
  recheck();
  for (const item of pending) replace(item.target, item.body);
  if (pending.length) {
    observed.clear();
    physicalAncestors(root);
    inspect(root, true);
    check(read(path.join(root, 'package.json')).equals(packageBody) && read(path.join(root, 'package-lock.json')).equals(lockBody), 'project-postcheck');
    check(read(SELF).equals(input.helper) && read(path.join(HOME, 'manifest.json')).equals(input.body) && read(path.join(HOME, input.policy.patch.path)).equals(input.patch), 'source-postcheck');
    const post = topology(root);
    check(canonical([...post.keys()].sort()) === canonical(nodes.map(node => node.path)), 'topology-postcheck');
    for (const node of nodes) {
      const files = inventory(post.get(node.path), input.policy);
      for (const row of node.files) check(matches(files.get(row.path), row), 'installed-postcheck');
    }
    recheck();
  }
  process.stdout.write(proof);
}

try { main(); }
catch (error) {
  // No untrusted file contents, environment values or filesystem paths in errors.
  const code = error instanceof Error && /^[a-z][a-z-]{0,60}$/.test(error.message) ? error.message : 'input-or-io-error';
  process.stderr.write('braces-backport: ' + code + '\n');
  process.exitCode = 1;
}
