import {
    lstatSync,
    realpathSync,
} from 'node:fs';
import path from 'node:path';

function isWithin(root: string, candidate: string): boolean {
    const relative = path.relative(root, candidate);
    return relative === ''
        || (
            relative !== '..'
            && !relative.startsWith(`..${path.sep}`)
            && !path.isAbsolute(relative)
        );
}

// 不跟隨連結：指向不存在位置的連結也算存在，之後 realpath 解不開就拒絕（ADR-0294）
function exists(candidate: string): boolean {
    try {
        lstatSync(candidate);
        return true;
    } catch (error: unknown) {
        const code = (error as NodeJS.ErrnoException).code;
        if (code === 'ENOENT' || code === 'ENOTDIR') {
            return false;
        }
        throw error;
    }
}

function nearestExistingPath(candidate: string): string {
    let current = candidate;
    while (!exists(current)) {
        const parent = path.dirname(current);
        if (parent === current) {
            throw new Error('No existing workspace ancestor found');
        }
        current = parent;
    }
    return current;
}

export function resolveWorkspacePath(
    workspaceRoots: readonly string[],
    requestedPath: string,
): string {
    if (workspaceRoots.length === 0) {
        throw new Error('No workspace folder is open');
    }

    const roots = workspaceRoots.map((root) => path.resolve(root));
    const candidate = path.resolve(
        path.isAbsolute(requestedPath) ? requestedPath : path.join(roots[0], requestedPath),
    );
    const lexicalRoot = roots.find((root) => isWithin(root, candidate));
    if (!lexicalRoot) {
        throw new Error('Path is outside the active workspace');
    }

    const realRoot = realpathSync.native(lexicalRoot);
    const existingAncestor = nearestExistingPath(candidate);
    const realAncestor = realpathSync.native(existingAncestor);
    if (!isWithin(realRoot, realAncestor)) {
        throw new Error('Path resolves outside the active workspace');
    }

    const unresolvedSuffix = path.relative(existingAncestor, candidate);
    const canonicalCandidate = path.resolve(realAncestor, unresolvedSuffix);
    if (!isWithin(realRoot, canonicalCandidate)) {
        throw new Error('Path resolves outside the active workspace');
    }
    return canonicalCandidate;
}
