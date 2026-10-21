import * as vscode from 'vscode';

export interface GitExtension {
    getAPI(version: number): GitAPI;
}

export interface GitAPI {
    state: string;
    repositories: Repository[];
}

export interface Repository {
    rootUri: vscode.Uri;
    inputBox: { value: string };
    state: {
        workingTreeChanges: Change[];
        indexChanges: Change[];
        mergeChanges: Change[];
    };
    commit(message: string, opts?: { all?: boolean, amend?: boolean, signoff?: boolean, signCommit?: boolean, empty?: boolean }): Promise<void>;
    diff(cached?: boolean): Promise<string>;
}

export interface Change {
    uri: vscode.Uri;
    renameUri?: vscode.Uri;
    status: number;
}
