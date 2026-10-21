/** Build-only Linux native readiness; all acquisition remains bound to npm's lock. */
import { execFileSync } from "node:child_process";
import {
  constants,
  copyFileSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, URL } from "node:url";

const NATIVE = "lightningcss-linux-x64-gnu";
const BINARY = "lightningcss.linux-x64-gnu.node";
const MAX_OUTPUT = 1024 * 1024;
const POLICY =
  "audit=false\nengine-strict=true\nfund=false\nignore-scripts=true\nregistry=https://registry.npmjs.org/\nupdate-notifier=false\n";

class NativeGateError extends Error {
  constructor(code, primary = "UNKNOWN") {
    super(code);
    this.code = code;
    this.primary = primary;
  }
}

function requireCondition(condition, code) {
  if (!condition) throw new NativeGateError(code);
}

export function closedNodeCode(error) {
  return [
    "MODULE_NOT_FOUND",
    "ERR_DLOPEN_FAILED",
    "ETIMEDOUT",
    "ENOBUFS",
    "EINTEGRITY",
  ].includes(error?.code)
    ? error.code
    : "UNKNOWN";
}

export function requiredLock(lock) {
  const fail = "NATIVE_LOCK_INVALID";
  const parent = lock?.packages?.["node_modules/lightningcss"];
  const native = lock?.packages?.[`node_modules/${NATIVE}`];
  requireCondition(lock?.lockfileVersion === 3 && parent && native, fail);
  const version = parent.optionalDependencies?.[NATIVE];
  requireCondition(
    typeof version === "string" && /^\d+\.\d+\.\d+$/.test(version),
    fail,
  );
  requireCondition(
    parent.version === version && native.version === version,
    fail,
  );
  requireCondition(
    JSON.stringify(native.os) === '["linux"]' &&
      JSON.stringify(native.cpu) === '["x64"]',
    fail,
  );
  const unsupported = [
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
    "bundleDependencies",
    "bundledDependencies",
    "link",
    "inBundle",
    "hasInstallScript",
  ];
  requireCondition(
    !unsupported.some((key) => Object.hasOwn(native, key)),
    fail,
  );
  requireCondition(
    native.resolved ===
      `https://registry.npmjs.org/${NATIVE}/-/${NATIVE}-${version}.tgz`,
    fail,
  );
  requireCondition(
    typeof native.integrity === "string" &&
      /^sha512-[A-Za-z0-9+/]{86}==$/.test(native.integrity),
    fail,
  );
  const digest = Buffer.from(native.integrity.slice(7), "base64");
  requireCondition(
    digest.length === 64 &&
      `sha512-${digest.toString("base64")}` === native.integrity,
    fail,
  );
  const record = Object.fromEntries(
    ["version", "resolved", "integrity", "os", "cpu", "engines", "license"]
      .filter((key) => Object.hasOwn(native, key))
      .map((key) => [key, native[key]]),
  );
  const packageJson = {
    name: "touka-critical-native-build",
    version: "0.0.0",
    private: true,
    dependencies: { [NATIVE]: version },
  };
  return {
    name: NATIVE,
    version,
    packageJson,
    lockfile: {
      name: packageJson.name,
      version: packageJson.version,
      lockfileVersion: 3,
      requires: true,
      packages: { "": packageJson, [`node_modules/${NATIVE}`]: record },
    },
  };
}

export function npmArguments({ npmCli, cache, offline }) {
  return [
    npmCli,
    "ci",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
    "--fetch-retries=0",
    "--fetch-timeout=30000",
    `--cache=${cache}`,
    ...(offline ? ["--offline"] : []),
  ];
}

function regular(pathname, { directory = false, absent = false } = {}) {
  let status;
  try {
    status = lstatSync(pathname);
  } catch (error) {
    if (absent && error.code === "ENOENT") return false;
    throw new NativeGateError("NATIVE_LAYOUT_INVALID");
  }
  requireCondition(
    !status.isSymbolicLink() &&
      (directory ? status.isDirectory() : status.isFile()),
    "NATIVE_LAYOUT_INVALID",
  );
  return true;
}

function projectDirectory(project) {
  requireCondition(path.isAbsolute(project), "NATIVE_LAYOUT_INVALID");
  regular(project, { directory: true });
  requireCondition(realpathSync(project) === project, "NATIVE_LAYOUT_INVALID");
  regular(path.join(project, "package.json"));
  regular(path.join(project, "node_modules"), { directory: true });
}

function packageMetadata(directory, spec, { complete = false } = {}) {
  regular(directory, { directory: true });
  const metadataFile = path.join(directory, "package.json");
  regular(metadataFile);
  const metadata = JSON.parse(readFileSync(metadataFile, "utf8"));
  requireCondition(
    metadata.name === spec.name &&
      metadata.version === spec.version &&
      metadata.main === BINARY,
    "NATIVE_LAYOUT_INVALID",
  );
  requireCondition(
    ![
      "dependencies",
      "optionalDependencies",
      "peerDependencies",
      "scripts",
      "bundleDependencies",
      "bundledDependencies",
    ].some((key) => Object.hasOwn(metadata, key)),
    "NATIVE_LAYOUT_INVALID",
  );
  regular(path.join(directory, BINARY));
  if (complete) {
    const names = readdirSync(directory).sort();
    requireCondition(
      JSON.stringify(names) ===
        JSON.stringify(["LICENSE", "README.md", BINARY, "package.json"].sort()),
      "NATIVE_LAYOUT_INVALID",
    );
    for (const name of names) regular(path.join(directory, name));
    requireCondition(
      JSON.stringify(metadata.os) === '["linux"]' &&
        JSON.stringify(metadata.cpu) === '["x64"]' &&
        JSON.stringify(metadata.libc) === '["glibc"]',
      "NATIVE_LAYOUT_INVALID",
    );
  }
  return metadata;
}

export function inspectNative(project, spec) {
  projectDirectory(project);
  const parentDirectory = path.join(project, "node_modules", "lightningcss");
  regular(parentDirectory, { directory: true });
  regular(path.join(parentDirectory, "package.json"));
  const parentMetadata = JSON.parse(
    readFileSync(path.join(parentDirectory, "package.json"), "utf8"),
  );
  requireCondition(
    parentMetadata.name === "lightningcss" &&
      parentMetadata.version === spec.version,
    "NATIVE_LAYOUT_INVALID",
  );
  const wrapper = path.join(parentDirectory, "node", "index.js");
  regular(path.dirname(wrapper), { directory: true });
  regular(wrapper);
  const projectRequire = createRequire(path.join(project, "package.json"));
  requireCondition(
    projectRequire.resolve("lightningcss") === wrapper &&
      realpathSync(wrapper) === wrapper,
    "NATIVE_LAYOUT_INVALID",
  );
  const directory = path.join(project, "node_modules", spec.name);
  const wrapperRequire = createRequire(wrapper);
  if (!regular(directory, { directory: true, absent: true })) {
    try {
      wrapperRequire.resolve(spec.name);
      throw new NativeGateError("NATIVE_LAYOUT_INVALID");
    } catch (error) {
      if (error.code === "MODULE_NOT_FOUND")
        return { missing: true, primary: "MODULE_NOT_FOUND" };
      throw error;
    }
  }
  packageMetadata(directory, spec);
  let native;
  try {
    const resolved = wrapperRequire.resolve(spec.name);
    requireCondition(
      resolved === path.join(directory, BINARY) &&
        realpathSync(resolved) === resolved,
      "NATIVE_LAYOUT_INVALID",
    );
    native = wrapperRequire(spec.name);
  } catch (error) {
    if (error instanceof NativeGateError) throw error;
    throw new NativeGateError("NATIVE_LOAD_FAILED", closedNodeCode(error));
  }
  try {
    requireCondition(
      typeof native.transform === "function",
      "NATIVE_TRANSFORM_FAILED",
    );
    const loadedWrapper = projectRequire("lightningcss");
    const result = loadedWrapper.transform({
      filename: "native-readiness.css",
      code: Buffer.from(".probe { color: red; }"),
      minify: true,
    });
    requireCondition(
      Buffer.isBuffer(result.code) &&
        result.code.toString() === ".probe{color:red}",
      "NATIVE_TRANSFORM_FAILED",
    );
  } catch (error) {
    if (error instanceof NativeGateError) throw error;
    throw new NativeGateError("NATIVE_TRANSFORM_FAILED", closedNodeCode(error));
  }
  return { missing: false, primary: null };
}

export function materializeNative(source, destination, spec) {
  try {
    lstatSync(destination);
    throw new NativeGateError("NATIVE_TARGET_EXISTS");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  packageMetadata(source, spec, { complete: true });
  try {
    mkdirSync(destination);
  } catch (error) {
    throw new NativeGateError(
      error.code === "EEXIST"
        ? "NATIVE_TARGET_EXISTS"
        : "NATIVE_MATERIALIZE_FAILED",
    );
  }
  // Destination is now owned by this invocation. Existing or racing paths were rejected.
  try {
    for (const name of readdirSync(source))
      copyFileSync(
        path.join(source, name),
        path.join(destination, name),
        constants.COPYFILE_EXCL,
      );
    packageMetadata(destination, spec, { complete: true });
  } catch (error) {
    rmSync(destination, { recursive: true });
    if (error instanceof NativeGateError) throw error;
    throw new NativeGateError("NATIVE_MATERIALIZE_FAILED");
  }
}

function closedEnvironment(home, userConfig, globalConfig, offline) {
  const environment = {};
  for (const key of [
    "PATH",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
  ]) {
    if (process.env[key] !== undefined) environment[key] = process.env[key];
  }
  return {
    ...environment,
    HOME: home,
    npm_config_userconfig: userConfig,
    npm_config_globalconfig: globalConfig,
    npm_config_offline: String(offline),
    npm_config_fetch_retries: "0",
    npm_config_update_notifier: "false",
  };
}

export function validateScratchPackages(scratch, spec) {
  const modules = path.join(scratch, "node_modules");
  regular(modules, { directory: true });
  const entries = readdirSync(modules);
  requireCondition(
    entries.includes(spec.name) &&
      entries.every((name) => [spec.name, ".package-lock.json"].includes(name)),
    "NATIVE_SCRATCH_LAYOUT_INVALID",
  );
  regular(path.join(modules, spec.name), { directory: true });
  if (entries.includes(".package-lock.json"))
    regular(path.join(modules, ".package-lock.json"));
}

export function verifyInstalled(
  { project, npm, cache, offline },
  spec,
  execute = execFileSync,
) {
  // Node can cache package resolution misses. A verifier never installs packages.
  const environment = Object.fromEntries(
    ["PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]
      .filter((key) => process.env[key] !== undefined)
      .map((key) => [key, process.env[key]]),
  );
  try {
    const output = execute(
      process.execPath,
      [
        path.join(project, "scripts", "ensure-critical-native.mjs"),
        "--npm",
        npm,
        "--cache",
        path.resolve(cache),
        "--verify-only",
        ...(offline ? ["--offline"] : []),
      ],
      {
        cwd: project,
        env: environment,
        timeout: 30000,
        maxBuffer: MAX_OUTPUT,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const verified = JSON.parse(output);
    requireCondition(
      JSON.stringify(Object.keys(verified).sort()) ===
        JSON.stringify(
          [
            "schema_version",
            "status",
            "phase",
            "package",
            "version",
            "repaired",
            "offline",
          ].sort(),
        ) &&
        verified.schema_version === 1 &&
        verified.status === "PASS" &&
        verified.phase === "native-readiness" &&
        verified.package === spec.name &&
        verified.version === spec.version &&
        verified.repaired === false &&
        verified.offline === offline,
      "NATIVE_POST_INSTALL_VERIFY_FAILED",
    );
  } catch (error) {
    if (error instanceof NativeGateError) throw error;
    throw new NativeGateError(
      "NATIVE_POST_INSTALL_VERIFY_FAILED",
      closedNodeCode(error),
    );
  }
}

export function ensureCriticalNative({
  project,
  npmCli,
  cache,
  offline = false,
  verifyOnly = false,
}) {
  const manifestFile = path.join(project, "package.json");
  const lockFile = path.join(project, "package-lock.json");
  const beforeManifest = readFileSync(manifestFile);
  const beforeLock = readFileSync(lockFile);
  const manifest = JSON.parse(beforeManifest);
  const npm = realpathSync(npmCli);
  const npmPackage = JSON.parse(
    readFileSync(path.join(path.dirname(npm), "..", "package.json"), "utf8"),
  );
  requireCondition(
    path.basename(npm) === "npm-cli.js" &&
      npmPackage.name === "npm" &&
      npmPackage.version === manifest.engines.npm,
    "NATIVE_TOOLCHAIN_INVALID",
  );
  requireCondition(
    process.platform === "linux" &&
      process.arch === "x64" &&
      typeof process.report.getReport().header.glibcVersionRuntime ===
        "string" &&
      process.versions.node === manifest.engines.node,
    "NATIVE_TOOLCHAIN_INVALID",
  );
  const spec = requiredLock(JSON.parse(beforeLock));
  let repaired = false;
  try {
    const before = inspectNative(project, spec);
    if (before.missing) {
      requireCondition(!verifyOnly, "NATIVE_VERIFY_MISSING");
      const scratch = mkdtempSync(
        path.join(tmpdir(), "touka-critical-native-"),
      );
      try {
        const userConfig = path.join(scratch, ".npmrc");
        const globalConfig = path.join(scratch, "empty-npmrc");
        writeFileSync(
          path.join(scratch, "package.json"),
          JSON.stringify(spec.packageJson),
          { flag: "wx" },
        );
        writeFileSync(
          path.join(scratch, "package-lock.json"),
          JSON.stringify(spec.lockfile),
          { flag: "wx" },
        );
        writeFileSync(userConfig, POLICY, { flag: "wx" });
        writeFileSync(globalConfig, "", { flag: "wx" });
        try {
          execFileSync(
            process.execPath,
            npmArguments({ npmCli: npm, cache: path.resolve(cache), offline }),
            {
              cwd: scratch,
              env: closedEnvironment(
                scratch,
                userConfig,
                globalConfig,
                offline,
              ),
              timeout: 30000,
              maxBuffer: MAX_OUTPUT,
              stdio: ["ignore", "pipe", "pipe"],
            },
          );
        } catch (error) {
          throw new NativeGateError(
            "NATIVE_REQUIRED_INSTALL_FAILED",
            closedNodeCode(error),
          );
        }
        validateScratchPackages(scratch, spec);
        materializeNative(
          path.join(scratch, "node_modules", spec.name),
          path.join(project, "node_modules", spec.name),
          spec,
        );
        repaired = true;
      } finally {
        rmSync(scratch, { recursive: true });
      }
      verifyInstalled({ project, npm, cache, offline }, spec);
    }
    return {
      schema_version: 1,
      status: "PASS",
      phase: "native-readiness",
      package: spec.name,
      version: spec.version,
      repaired,
      offline,
    };
  } finally {
    requireCondition(
      readFileSync(manifestFile).equals(beforeManifest) &&
        readFileSync(lockFile).equals(beforeLock),
      "NATIVE_PROJECT_INPUT_CHANGED",
    );
  }
}

function main(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    requireCondition(
      ["--npm", "--cache", "--offline", "--verify-only"].includes(argument) &&
        !Object.hasOwn(options, argument),
      "NATIVE_ARGUMENT_INVALID",
    );
    if (argument === "--offline" || argument === "--verify-only")
      options[argument] = true;
    else {
      index += 1;
      requireCondition(
        typeof argv[index] === "string" && !argv[index].startsWith("--"),
        "NATIVE_ARGUMENT_INVALID",
      );
      options[argument] = argv[index];
    }
  }
  requireCondition(
    options["--npm"] && options["--cache"],
    "NATIVE_ARGUMENT_INVALID",
  );
  const project = fileURLToPath(new URL("../", import.meta.url)).replace(
    /[\\/]$/,
    "",
  );
  return ensureCriticalNative({
    project,
    npmCli: options["--npm"],
    cache: options["--cache"],
    offline: options["--offline"] === true,
    verifyOnly: options["--verify-only"] === true,
  });
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    process.stdout.write(`${JSON.stringify(main(process.argv.slice(2)))}\n`);
  } catch (error) {
    const result = {
      schema_version: 1,
      status: "FAIL",
      phase: "native-readiness",
      code:
        error instanceof NativeGateError ? error.code : "NATIVE_INTERNAL_ERROR",
      primary:
        error instanceof NativeGateError
          ? error.primary
          : closedNodeCode(error),
    };
    process.stderr.write(`${JSON.stringify(result)}\n`);
    process.exitCode = 1;
  }
}
