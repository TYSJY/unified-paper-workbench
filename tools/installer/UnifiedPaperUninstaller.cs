using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class UnifiedPaperUninstaller
{
    private const string ProductName = "Unified Paper Workbench";
    private const string ExecutableName = "UnifiedPaperOrchestrator.exe";
    private const string UninstallerName = "Uninstall.exe";
    private const string InstallMarkerName = ".unified-paper-workbench-install";
    private const string InstallMarkerContent = "UnifiedPaperWorkbenchInstallRoot/v1";
    private const string RegistryPath =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\UnifiedPaperWorkbench";
    private const int MoveFileDelayUntilReboot = 0x4;

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool MoveFileEx(
        string existingFileName,
        string newFileName,
        int flags
    );

    [STAThread]
    private static int Main(string[] args)
    {
        string cleanupDirectory = GetArgumentValue(args, "/cleanup-install-dir=");
        if (cleanupDirectory != null)
        {
            return CleanupInstallDirectory(
                cleanupDirectory,
                GetIntegerArgument(args, "/wait-pid=")
            );
        }

        Application.EnableVisualStyles();
        bool silent = args.Any(
            value => string.Equals(value, "/silent", StringComparison.OrdinalIgnoreCase)
        );
        bool removeRegistry = !args.Any(
            value =>
                string.Equals(value, "/no-registry", StringComparison.OrdinalIgnoreCase)
        );
        if (!silent)
        {
            DialogResult answer = MessageBox.Show(
                "仅卸载应用程序；论文工作区、实验数据和用户数据将保留。\n\n"
                    + "Uninstall the application while keeping all workspaces and user data?",
                ProductName,
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Question
            );
            if (answer != DialogResult.OK)
            {
                return 1;
            }
        }

        try
        {
            string installDirectory = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
                Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar
            );
            RequireOwnedInstallDirectory(installDirectory);
            RemoveShortcut(
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                    ProductName + ".lnk"
                )
            );
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                "Programs",
                ProductName
            );
            if (Directory.Exists(startMenu))
            {
                Directory.Delete(startMenu, true);
            }
            if (removeRegistry)
            {
                Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
            }
            ScheduleDirectoryRemoval(installDirectory);
            if (!silent)
            {
                MessageBox.Show(
                    "应用程序已卸载；论文与用户数据仍然保留。\n\n"
                        + "The application was removed; workspaces and user data were preserved.",
                    ProductName,
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information
                );
            }
            return 0;
        }
        catch (Exception error)
        {
            if (!silent)
            {
                MessageBox.Show(
                    "卸载未完成。\n\nUninstall did not complete.\n\n" + error.Message,
                    ProductName,
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
            return 2;
        }
    }

    private static void RemoveShortcut(string path)
    {
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }

    private static void ScheduleDirectoryRemoval(string installDirectory)
    {
        string helper = Path.Combine(
            Path.GetTempPath(),
            "UnifiedPaperWorkbench-uninstall-" + Guid.NewGuid().ToString("N") + ".exe"
        );
        File.Copy(Application.ExecutablePath, helper, false);
        Process.Start(
            new ProcessStartInfo
            {
                FileName = helper,
                Arguments = string.Join(
                    " ",
                    QuoteWindowsArgument("/cleanup-install-dir=" + installDirectory),
                    QuoteWindowsArgument(
                        "/wait-pid=" + Process.GetCurrentProcess().Id.ToString()
                    )
                ),
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden,
            }
        );
    }

    private static int CleanupInstallDirectory(string value, int waitPid)
    {
        try
        {
            string installDirectory = Path.GetFullPath(value);
            RequireOwnedInstallDirectory(installDirectory);
            if (waitPid > 0)
            {
                try
                {
                    using (Process parent = Process.GetProcessById(waitPid))
                    {
                        if (!parent.WaitForExit(30000))
                        {
                            return 3;
                        }
                    }
                }
                catch (ArgumentException)
                {
                    // The original uninstaller has already exited.
                }
            }
            for (int attempt = 0; attempt < 20; attempt += 1)
            {
                try
                {
                    RequireOwnedInstallDirectory(installDirectory);
                    Directory.Delete(installDirectory, true);
                    MoveFileEx(
                        Application.ExecutablePath,
                        null,
                        MoveFileDelayUntilReboot
                    );
                    return 0;
                }
                catch (IOException)
                {
                    Thread.Sleep(250);
                }
                catch (UnauthorizedAccessException)
                {
                    Thread.Sleep(250);
                }
            }
            return 4;
        }
        catch
        {
            return 2;
        }
    }

    private static void RequireOwnedInstallDirectory(string installDirectory)
    {
        string rootPath = Path.GetPathRoot(installDirectory);
        if (string.Equals(installDirectory, rootPath, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Refusing to remove a drive root.");
        }
        DirectoryInfo root = new DirectoryInfo(installDirectory);
        if (!root.Exists || (root.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidOperationException(
                "The target is not a safe Unified Paper Workbench installation."
            );
        }
        RequireFile(Path.Combine(installDirectory, ExecutableName));
        RequireFile(Path.Combine(installDirectory, UninstallerName));
        string marker = Path.Combine(installDirectory, InstallMarkerName);
        if (
            !File.Exists(marker)
            || !string.Equals(
                File.ReadAllText(marker),
                InstallMarkerContent,
                StringComparison.Ordinal
            )
        ) {
            throw new InvalidOperationException(
                "The target is not a Unified Paper Workbench installation."
            );
        }
        foreach (FileSystemInfo item in root.EnumerateFileSystemInfos("*", SearchOption.AllDirectories))
        {
            if ((item.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidOperationException(
                    "The installation contains an unsafe reparse point."
                );
            }
        }
    }

    private static void RequireFile(string path)
    {
        if (!File.Exists(path))
        {
            throw new InvalidOperationException(
                "The target is not a Unified Paper Workbench installation."
            );
        }
    }

    private static string GetArgumentValue(string[] args, string prefix)
    {
        string argument = args.FirstOrDefault(
            value => value.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
        );
        if (argument == null)
        {
            return null;
        }
        string cleanValue = argument.Substring(prefix.Length).Trim().Trim('"');
        return cleanValue.Length == 0 ? null : cleanValue;
    }

    private static int GetIntegerArgument(string[] args, string prefix)
    {
        int value;
        return int.TryParse(GetArgumentValue(args, prefix), out value) ? value : 0;
    }

    private static string QuoteWindowsArgument(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes += 1;
                continue;
            }
            if (character == '"')
            {
                result.Append('\\', (backslashes * 2) + 1);
                result.Append('"');
                backslashes = 0;
                continue;
            }
            result.Append('\\', backslashes);
            backslashes = 0;
            result.Append(character);
        }
        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }
}
