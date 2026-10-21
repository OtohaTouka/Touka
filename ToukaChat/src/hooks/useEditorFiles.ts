"use client";
import { useState, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

export function useEditorFiles() {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [files, setFiles] = useState<any[]>([]);
    const [isLoading, setIsLoading] = useState(false);

    useEffect(() => {
        const fetchFiles = async () => {
            setIsLoading(true);
            try {
                const proxyUrl = getActiveProxyUrl();
                const res = await fetchTouka(proxyUrl, "/editor/files");
                if (res.ok) {
                    const data = await res.json();
                    if (data.files) {
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        setFiles(data.files.map((f: any) => ({ ...f, type: "file" })));
                    }
                }
            } catch (err) {
                logSSOTError(err, "useEditorFiles:fetchFiles");
                setFiles([]);
            } finally {
                setIsLoading(false);
            }
        };
        fetchFiles();
    }, []);

    return { files, isLoading };
}
