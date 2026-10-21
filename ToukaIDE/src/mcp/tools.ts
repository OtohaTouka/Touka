/**
 * MCP Tools schema definitions for ToukaIDE
 */
import type { JsonSchemaType } from '@modelcontextprotocol/server';

export interface IdeToolDefinition {
    name: string;
    description: string;
    inputSchema: JsonSchemaType;
}

export const tools: readonly IdeToolDefinition[] = [
    {
        name: "get_workspace_structure",
        description: "Returns the directory tree of the current VS Code workspace.",
        inputSchema: {
            type: "object",
            properties: {
                depth: {
                    type: "number",
                    description: "Depth of the directory tree to return.",
                    default: 3
                }
            }
        }
    },
    {
        name: "get_diagnostics",
        description: "Gets the errors and warnings (diagnostics) from the VS Code Problems panel.",
        inputSchema: {
            type: "object",
            properties: {
                uri: {
                    type: "string",
                    description: "Optional file URI to filter diagnostics."
                }
            }
        }
    },
    {
        name: "get_open_file",
        description: "Gets the full text content of the currently active file in the editor, along with the cursor position and visible range.",
        inputSchema: {
            type: "object",
            properties: {}
        }
    },
    {
        name: "get_recent_files",
        description: "Gets the list of up to 10 recently accessed files in the editor.",
        inputSchema: {
            type: "object",
            properties: {}
        }
    },
    {
        name: "get_selection",
        description: "Gets the currently selected text in the active editor.",
        inputSchema: {
            type: "object",
            properties: {}
        }
    },
    {
        name: "read_file",
        description: "Reads the content of a specified file path.",
        inputSchema: {
            type: "object",
            properties: {
                file_path: {
                    type: "string",
                    description: "The absolute or relative path of the file to read."
                }
            },
            required: ["file_path"]
        }
    },
    {
        name: "list_directory",
        description: "Lists files and folders in a specified directory path.",
        inputSchema: {
            type: "object",
            properties: {
                dir_path: {
                    type: "string",
                    description: "The absolute or relative path of the directory to list."
                }
            },
            required: ["dir_path"]
        }
    },
    {
        name: "write_file",
        description: "Writes complete content to a file. This will open a native Diff view in the editor for the user to review and accept the changes before they are applied.",
        inputSchema: {
            type: "object",
            properties: {
                file_path: {
                    type: "string",
                    description: "The absolute or relative path of the file to write."
                },
                content: {
                    type: "string",
                    description: "The text content to write to the file."
                }
            },
            required: ["file_path", "content"]
        }
    },
    {
        name: "execute_terminal",
        description: "Executes a command in the integrated terminal and returns the stdout, stderr, and EXIT_CODE. Requires user confirmation. Use this to run scripts, tests, or manage the environment. If the EXIT_CODE is not 0, analyze the stderr to perform self-correction in the next ReAct turn.",
        inputSchema: {
            type: "object",
            properties: {
                command: {
                    type: "string",
                    description: "The command to execute."
                }
            },
            required: ["command"]
        }
    },
    {
        name: "get_git_changes",
        description: "Gets current Git changes (staged and unstaged) and diffs using VS Code's native Git API.",
        inputSchema: {
            type: "object",
            properties: {}
        }
    },
    {
        name: "git_commit",
        description: "Stages all changes and commits them with the provided message.",
        inputSchema: {
            type: "object",
            properties: {
                message: {
                    type: "string",
                    description: "The commit message to use."
                }
            },
            required: ["message"]
        }
    },
    {
        name: "create_plan",
        description: "Creates a multi-step execution plan to be visualized in the UI.",
        inputSchema: {
            type: "object",
            properties: {
                title: {
                    type: "string",
                    description: "The title of the plan."
                },
                steps: {
                    type: "array",
                    items: { type: "string" },
                    description: "A list of steps in the plan."
                }
            },
            required: ["title", "steps"]
        }
    },
    {
        name: "update_task_status",
        description: "Updates the status of a specific step in the current plan.",
        inputSchema: {
            type: "object",
            properties: {
                step_index: {
                    type: "number",
                    description: "The 0-based index of the step to update."
                },
                status: {
                    type: "string",
                    enum: ["todo", "in_progress", "completed", "failed"],
                    description: "The new status of the step."
                },
                details: {
                    type: "string",
                    description: "Optional details about the current progress."
                }
            },
            required: ["step_index", "status"]
        }
    }
];
