/* Deliver a protocol callback to the RUNNING Meta client; never start a second Client.exe.
 * UTF-8 stdin is preferred (no credentials in process argv). argv[1] is useful for non-secret device probes.
 * Uses a NUL-terminated UTF-8 link. Device verification of the Oculus pipe's framing is still required.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    char link[65537];
    size_t len;
    HANDLE pipe = INVALID_HANDLE_VALUE;
    DWORD written;
    if (argc == 2) {
        len = strlen(argv[1]);
        if (len >= sizeof(link)) return 2;
        memcpy(link, argv[1], len);
    } else if (argc == 1) {
        len = fread(link, 1, sizeof(link), stdin);
        if (ferror(stdin) || len >= sizeof(link)) return 2;
    } else return 2;
    if (len < 24 || strncmp(link, "oculus://link/frl_login?", 24) || memchr(link, 0, len)) return 2;
    link[len] = 0;
    for (int i = 0; i < 100; ++i) {
        pipe = CreateFileW(L"\\\\.\\pipe\\oculus", GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
        if (pipe != INVALID_HANDLE_VALUE) break;
        DWORD err = GetLastError();
        if (err != ERROR_PIPE_BUSY && err != ERROR_FILE_NOT_FOUND) return 3;
        if (err == ERROR_PIPE_BUSY) WaitNamedPipeW(L"\\\\.\\pipe\\oculus", 200);
        else Sleep(200);
    }
    if (pipe == INVALID_HANDLE_VALUE) return 3;
    BOOL ok = WriteFile(pipe, link, (DWORD)len + 1, &written, NULL);
    CloseHandle(pipe);
    SecureZeroMemory(link, sizeof(link));
    return ok && written == len + 1 ? 0 : 4;
}
