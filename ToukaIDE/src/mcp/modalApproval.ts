import { AuthLevel, needsIdeConfirmation } from './authPolicy';

export interface ModalOptions {
    modal: true;
    detail: string;
}

export type ModalPresenter = (
    message: string,
    options: ModalOptions,
    ...items: string[]
) => Promise<string | undefined>;

export async function requestIdeApproval(
    authLevel: AuthLevel,
    humanApproved: boolean,
    title: string,
    detail: string,
    present: ModalPresenter,
): Promise<boolean> {
    if (!needsIdeConfirmation(authLevel, humanApproved)) {
        return true;
    }
    const result = await present(
        `Touka 請求授權: ${title}`,
        { modal: true, detail },
        '允許',
        '拒絕',
    );
    return result === '允許';
}
