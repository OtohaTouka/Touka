import * as fs from 'node:fs';
import * as path from 'node:path';

import * as vscode from 'vscode';

import { AuthLevel, needsIdeConfirmation } from '../authPolicy';
import { diffManager } from '../diffManager';
import { readFileNoFollow, writeFileNoFollow } from '../noFollowFile';
import { resolveWorkspacePath } from '../workspacePath';

function workspaceRoots(): string[] {
    return (vscode.workspace.workspaceFolders ?? [])
        .map((folder) => folder.uri.fsPath);
}

function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

export async function handleGetWorkspaceStructure(
    depth: number = 3,
): Promise<string> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders) {
        return 'No workspace folders open.';
    }

    let structure = '';
    for (const folder of workspaceFolders) {
        structure += `Folder: ${folder.name}\n`;
        structure += await getDirectoryTree(folder.uri.fsPath, '', 0, depth);
    }
    return structure;
}

async function getDirectoryTree(
    dirPath: string,
    indent: string,
    currentDepth: number,
    maxDepth: number,
): Promise<string> {
    if (currentDepth >= maxDepth) {
        return '';
    }
    let tree = '';
    try {
        const files = await fs.promises.readdir(dirPath, { withFileTypes: true });
        for (const file of files) {
            if (['node_modules', '.git', '.next'].includes(file.name)) {
                continue;
            }
            tree += `${indent}${file.isDirectory() ? '📁' : '📄'} ${file.name}\n`;
            if (file.isDirectory()) {
                tree += await getDirectoryTree(
                    path.join(dirPath, file.name),
                    `${indent}  `,
                    currentDepth + 1,
                    maxDepth,
                );
            }
        }
    } catch {
        return tree;
    }
    return tree;
}

export async function handleReadFile(filePath: string): Promise<string> {
    try {
        const absolutePath = resolveWorkspacePath(workspaceRoots(), filePath);
        return await readFileNoFollow(absolutePath);
    } catch (error: unknown) {
        return `Error reading file: ${errorMessage(error)}`;
    }
}

export async function handleListDirectory(dirPath: string): Promise<string> {
    try {
        const absolutePath = resolveWorkspacePath(workspaceRoots(), dirPath);
        const files = await fs.promises.readdir(absolutePath, { withFileTypes: true });
        const list = files
            .map((file) => `${file.isDirectory() ? '📁' : '📄'} ${file.name}`)
            .join('\n');
        return list || '(Empty directory)';
    } catch (error: unknown) {
        return `Error listing directory: ${errorMessage(error)}`;
    }
}

export async function handleWriteFile(
    filePath: string,
    content: string,
    authLevel: AuthLevel,
    humanApproved: boolean,
): Promise<string> {
    try {
        const absolutePath = resolveWorkspacePath(workspaceRoots(), filePath);
        if (!needsIdeConfirmation(authLevel, humanApproved)) {
            await fs.promises.mkdir(path.dirname(absolutePath), { recursive: true });
            await writeFileNoFollow(absolutePath, content);
            const reason = humanApproved ? 'approved on the Touka card' : `${authLevel} mode`;
            return `Successfully directly wrote to ${filePath} (${reason})`;
        }
        return await diffManager.proposeChanges(
            absolutePath,
            content,
            () => resolveWorkspacePath(workspaceRoots(), filePath),
        );
    } catch (error: unknown) {
        return `Error writing file: ${errorMessage(error)}`;
    }
}
