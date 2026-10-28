using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class UnifiedPaperInstaller
{
    private const string ProductName = "Unified Paper Workbench";
    private const string ExecutableName = "UnifiedPaperOrchestrator.exe";
    private const string UninstallerName = "Uninstall.exe";
    private const string InstallMarkerName = ".unified-paper-workbench-install";
    private const string InstallMarkerContent = "UnifiedPaperWorkbenchInstallRoot/v1";
    private const string RegistryPath =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\UnifiedPaperWorkbench";
    private const string TrailerMagic = "UPWPKG0120B3END";

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        bool silent = HasFlag(args, "/silent");
        if (HasFlag(args, "/verify"))
        {
            try
            {
                using (PackageArchive archive = PackageArchive.Open(Application.ExecutablePath))
                {
                    return archive.ContainsRequiredFiles() ? 0 : 3;
                }
            }
            catch
            {
                return 2;
            }
        }

        string installDirectory = GetArgumentValue(args, "/install-dir=")
            ?? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Programs",
                "UnifiedPaperWorkbench"
            );
        bool createShortcuts = !HasFlag(args, "/no-shortcuts");
        bool writeRegistry = !HasFlag(args, "/no-registry");
        bool launch = !HasFlag(args, "/no-launch");

        if (!silent)
        {
            DialogResult answer = MessageBox.Show(
                "将把 Unified Paper Workbench 安装到当前 Windows 用户。\n\n"
                    + "论文工作区、实验数据和现有用户数据不会被移动或删除。\n\n"
                    + "Install Unified Paper Workbench for the current Windows user?",
                ProductName,
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Information
            );
            if (answer != DialogResult.OK)
            {
                return 1;
            }
        }

        try
        {
            EnsureApplicationIsClosed();
            Install(installDirectory, createShortcuts, writeRegistry);
            if (writeRegistry)
            {
                PrepareFirstWorkspace(installDirectory);
            }
            if (!silent)
            {
                MessageBox.Show(
                    "安装完成。\n\nInstallation completed successfully.",
                    ProductName,
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information
                );
            }
            if (launch)
            {
                Process.Start(
                    new ProcessStartInfo
                    {
                        FileName = Path.Combine(installDirectory, ExecutableName),
                        WorkingDirectory = installDirectory,
                        UseShellExecute = true,
                    }
                );
            }
            return 0;
        }
        catch (Exception error)
        {
            WriteFailureLog(error);
            if (!silent)
            {
                MessageBox.Show(
                    "安装未完成，原有版本已尽量保留。\n"
                        + "Installation did not complete; the previous version was preserved where possible.\n\n"
                        + error.Message,
                    ProductName,
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
            return 2;
        }
    }

    private static void Install(
        string requestedDirectory,
        bool createShortcuts,
        bool writeRegistry
    )
    {
        string installDirectory = Path.GetFullPath(requestedDirectory);
        string root = Path.GetPathRoot(installDirectory);
        if (string.Equals(installDirectory, root, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "The installation directory cannot be a drive root."
            );
        }
        DirectoryInfo parentInfo = Directory.GetParent(installDirectory);
        if (parentInfo == null)
        {
            throw new InvalidOperationException("The installation directory is invalid.");
        }
        string parent = parentInfo.FullName;
        Directory.CreateDirectory(parent);

        string stagingDirectory = Path.Combine(
            parent,
            ".UnifiedPaperWorkbench-install-" + Guid.NewGuid().ToString("N")
        );
        string backupDirectory = Path.Combine(
            parent,
            ".UnifiedPaperWorkbench-backup-" + Guid.NewGuid().ToString("N")
        );
        bool previousMoved = false;
        try
        {
            Directory.CreateDirectory(stagingDirectory);
            using (PackageArchive archive = PackageArchive.Open(Application.ExecutablePath))
            {
                archive.ExtractSafely(stagingDirectory);
            }
            RequireFile(Path.Combine(stagingDirectory, ExecutableName));
            RequireFile(Path.Combine(stagingDirectory, UninstallerName));
            RequireInstallMarker(stagingDirectory);

            if (Directory.Exists(installDirectory))
            {
                if (Directory.EnumerateFileSystemEntries(installDirectory).Any())
                {
                    RequireOwnedInstallDirectory(installDirectory);
                }
                Directory.Move(installDirectory, backupDirectory);
                previousMoved = true;
            }
            Directory.Move(stagingDirectory, installDirectory);

            if (createShortcuts)
            {
                CreateShortcuts(installDirectory);
            }
            if (writeRegistry)
            {
                RegisterUninstaller(installDirectory);
            }
            if (previousMoved && Directory.Exists(backupDirectory))
            {
                try
                {
                    Directory.Delete(backupDirectory, true);
                }
                catch
                {
                    // A retained backup is safer than treating a successful
                    // replacement as failed and risking an incomplete restore.
                }
            }
        }
        catch
        {
            if (Directory.Exists(stagingDirectory))
            {
                Directory.Delete(stagingDirectory, true);
            }
            if (Directory.Exists(installDirectory))
            {
                RequireOwnedInstallDirectory(installDirectory);
                Directory.Delete(installDirectory, true);
            }
            if (
                previousMoved
                && Directory.Exists(backupDirectory)
            )
            {
                Directory.Move(backupDirectory, installDirectory);
            }
            throw;
        }
    }

    private static void RequireOwnedInstallDirectory(string installDirectory)
    {
        DirectoryInfo root = new DirectoryInfo(installDirectory);
        if ((root.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidOperationException(
                "The installation directory cannot be a reparse point."
            );
        }
        RequireFile(Path.Combine(installDirectory, ExecutableName));
        RequireFile(Path.Combine(installDirectory, UninstallerName));
        RequireInstallMarker(installDirectory);
        foreach (FileSystemInfo item in root.EnumerateFileSystemInfos("*", SearchOption.AllDirectories))
        {
            if ((item.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidOperationException(
                    "The installation directory contains an unsafe reparse point."
                );
            }
        }
    }

    private static void PrepareFirstWorkspace(string installDirectory)
    {
        string template = Path.Combine(installDirectory, "workspace-template");
        if (!File.Exists(Path.Combine(template, "workspace.json"))) return;
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string registry = Path.Combine(local, "UnifiedPaperOrchestrator", "workspaces.json");
        string workspace = Path.Combine(local, "UnifiedPaperWorkbench", "Workspaces", "Main");
        InitializeWorkspace(template, workspace, registry);
    }

    private static void InitializeWorkspace(string template, string workspace, string registry)
    {
        // Existing users keep their workspace selection and all manuscript data.
        if (File.Exists(registry)) return;
        if (Directory.Exists(workspace) && Directory.EnumerateFileSystemEntries(workspace).Any()) return;
        Directory.CreateDirectory(workspace);
        string prefix = Path.GetFullPath(workspace) + Path.DirectorySeparatorChar;
        foreach (string file in Directory.EnumerateFiles(template, "*", SearchOption.AllDirectories))
        {
            string relative = file.Substring(template.Length).TrimStart(Path.DirectorySeparatorChar);
            string destination = Path.GetFullPath(Path.Combine(workspace, relative));
            if (!destination.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Workspace template path escaped its root.");
            Directory.CreateDirectory(Path.GetDirectoryName(destination));
            File.Copy(file, destination, false);
        }
        Directory.CreateDirectory(Path.GetDirectoryName(registry));
        var data = new System.Collections.Generic.Dictionary<string, object>
        {
            {"schema", "unified-paper-orchestrator/workspace-registry/v1"},
            {"version", 1}, {"active_workspace_id", "public-main"},
            {"active_workspace_path", workspace},
            {"workspaces", new object[] {new System.Collections.Generic.Dictionary<string, object>
                {{"workspace_id", "public-main"}, {"name", "My Paper Workspace"},
                 {"path", workspace}, {"mode", "managed"}}}}
        };
        string serialized = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(data);
        using (var stream = new FileStream(registry, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        using (var writer = new StreamWriter(stream, new UTF8Encoding(false))) writer.Write(serialized);
    }

    private static void RequireInstallMarker(string installDirectory)
    {
        string marker = Path.Combine(installDirectory, InstallMarkerName);
        string content;
        try
        {
            content = File.ReadAllText(marker, Encoding.UTF8);
        }
        catch (Exception error)
        {
            throw new InvalidDataException(
                "The target is not a Unified Paper Workbench installation.",
                error
            );
        }
        if (!string.Equals(content, InstallMarkerContent, StringComparison.Ordinal))
        {
            throw new InvalidDataException(
                "The target is not a Unified Paper Workbench installation."
            );
        }
    }

    private static void EnsureApplicationIsClosed()
    {
        Process[] running = Process.GetProcessesByName(
            Path.GetFileNameWithoutExtension(ExecutableName)
        );
        try
        {
            if (running.Any())
            {
                throw new InvalidOperationException(
                    "请先关闭正在运行的 Unified Paper Workbench，然后重新安装。"
                );
            }
        }
        finally
        {
            foreach (Process process in running)
            {
                process.Dispose();
            }
        }
    }

    private static void CreateShortcuts(string installDirectory)
    {
        string target = Path.Combine(installDirectory, ExecutableName);
        string startMenuDirectory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
            "Programs",
            ProductName
        );
        Directory.CreateDirectory(startMenuDirectory);
        CreateShortcut(
            Path.Combine(startMenuDirectory, ProductName + ".lnk"),
            target,
            installDirectory
        );
        CreateShortcut(
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                ProductName + ".lnk"
            ),
            target,
            installDirectory
        );
    }

    private static void CreateShortcut(
        string shortcutPath,
        string targetPath,
        string workingDirectory
    )
    {
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType == null)
        {
            throw new InvalidOperationException("Windows shortcut service is unavailable.");
        }
        object shell = Activator.CreateInstance(shellType);
        object shortcut = null;
        try
        {
            shortcut = shellType.InvokeMember(
                "CreateShortcut",
                System.Reflection.BindingFlags.InvokeMethod,
                null,
                shell,
                new object[] { shortcutPath }
            );
            Type shortcutType = shortcut.GetType();
            shortcutType.InvokeMember(
                "TargetPath",
                System.Reflection.BindingFlags.SetProperty,
                null,
                shortcut,
                new object[] { targetPath }
            );
            shortcutType.InvokeMember(
                "WorkingDirectory",
                System.Reflection.BindingFlags.SetProperty,
                null,
                shortcut,
                new object[] { workingDirectory }
            );
            shortcutType.InvokeMember(
                "IconLocation",
                System.Reflection.BindingFlags.SetProperty,
                null,
                shortcut,
                new object[] { targetPath + ",0" }
            );
            shortcutType.InvokeMember(
                "Save",
                System.Reflection.BindingFlags.InvokeMethod,
                null,
                shortcut,
                null
            );
        }
        finally
        {
            if (shortcut != null && Marshal.IsComObject(shortcut))
            {
                Marshal.FinalReleaseComObject(shortcut);
            }
            if (shell != null && Marshal.IsComObject(shell))
            {
                Marshal.FinalReleaseComObject(shell);
            }
        }
    }

    private static void RegisterUninstaller(string installDirectory)
    {
        string executable = Path.Combine(installDirectory, ExecutableName);
        string uninstaller = Path.Combine(installDirectory, UninstallerName);
        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
        {
            key.SetValue("DisplayName", ProductName);
            key.SetValue("DisplayVersion", Application.ProductVersion);
            key.SetValue("Publisher", "Unified Paper");
            key.SetValue("InstallLocation", installDirectory);
            key.SetValue("DisplayIcon", executable);
            key.SetValue("UninstallString", Quote(uninstaller));
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
    }

    private static void RequireFile(string path)
    {
        if (!File.Exists(path))
        {
            throw new InvalidDataException(
                "The installer package is incomplete: " + Path.GetFileName(path)
            );
        }
    }

    private static bool HasFlag(string[] args, string flag)
    {
        return args.Any(
            value => string.Equals(value, flag, StringComparison.OrdinalIgnoreCase)
        );
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

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void WriteFailureLog(Exception error)
    {
        try
        {
            File.WriteAllText(
                Path.Combine(Path.GetTempPath(), "UnifiedPaperWorkbench-installer.log"),
                DateTime.UtcNow.ToString("O") + Environment.NewLine + error,
                Encoding.UTF8
            );
        }
        catch
        {
        }
    }

    private sealed class PackageArchive : IDisposable
    {
        private readonly FileStream executable;
        private readonly string temporaryZip;
        private readonly ZipArchive archive;

        private PackageArchive(FileStream executable, string temporaryZip, ZipArchive archive)
        {
            this.executable = executable;
            this.temporaryZip = temporaryZip;
            this.archive = archive;
        }

        public static PackageArchive Open(string executablePath)
        {
            FileStream executable = File.Open(
                executablePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read
            );
            string temporaryZip = null;
            try
            {
                byte[] magic = Encoding.ASCII.GetBytes(TrailerMagic);
                long trailerLength = sizeof(long) + magic.Length;
                if (executable.Length <= trailerLength)
                {
                    throw new InvalidDataException("Installer payload is missing.");
                }
                executable.Seek(-trailerLength, SeekOrigin.End);
                long payloadLength;
                byte[] observedMagic;
                using (
                    BinaryReader reader = new BinaryReader(
                        executable,
                        Encoding.ASCII,
                        true
                    )
                )
                {
                    payloadLength = reader.ReadInt64();
                    observedMagic = reader.ReadBytes(magic.Length);
                }
                if (
                    payloadLength <= 0
                    || payloadLength > executable.Length - trailerLength
                    || !observedMagic.SequenceEqual(magic)
                )
                {
                    throw new InvalidDataException("Installer payload is invalid.");
                }
                long payloadOffset = executable.Length - trailerLength - payloadLength;
                executable.Seek(payloadOffset, SeekOrigin.Begin);
                temporaryZip = Path.Combine(
                    Path.GetTempPath(),
                    "UnifiedPaperWorkbench-" + Guid.NewGuid().ToString("N") + ".zip"
                );
                using (
                    FileStream output = File.Open(
                        temporaryZip,
                        FileMode.CreateNew,
                        FileAccess.Write,
                        FileShare.None
                    )
                )
                {
                    CopyExactly(executable, output, payloadLength);
                }
                ZipArchive archive = ZipFile.OpenRead(temporaryZip);
                return new PackageArchive(executable, temporaryZip, archive);
            }
            catch
            {
                executable.Dispose();
                if (temporaryZip != null && File.Exists(temporaryZip))
                {
                    File.Delete(temporaryZip);
                }
                throw;
            }
        }

        public bool ContainsRequiredFiles()
        {
            return archive.Entries.Any(
                    entry =>
                        string.Equals(
                            entry.FullName,
                            ExecutableName,
                            StringComparison.OrdinalIgnoreCase
                        )
                )
                && archive.Entries.Any(
                    entry =>
                        string.Equals(
                            entry.FullName,
                            UninstallerName,
                            StringComparison.OrdinalIgnoreCase
                        )
                )
                && ArchiveMarkerIsValid();
        }

        private bool ArchiveMarkerIsValid()
        {
            ZipArchiveEntry marker = archive.Entries.FirstOrDefault(
                entry =>
                    string.Equals(
                        entry.FullName,
                        InstallMarkerName,
                        StringComparison.OrdinalIgnoreCase
                    )
            );
            if (marker == null)
            {
                return false;
            }
            using (StreamReader reader = new StreamReader(marker.Open(), Encoding.UTF8))
            {
                return string.Equals(
                    reader.ReadToEnd(),
                    InstallMarkerContent,
                    StringComparison.Ordinal
                );
            }
        }

        public void ExtractSafely(string destination)
        {
            string root = Path.GetFullPath(destination)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                string relative = entry.FullName.Replace(
                    Path.AltDirectorySeparatorChar,
                    Path.DirectorySeparatorChar
                );
                string outputPath = Path.GetFullPath(Path.Combine(root, relative));
                if (!outputPath.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidDataException("Installer archive path is unsafe.");
                }
                if (
                    relative.EndsWith(
                        Path.DirectorySeparatorChar.ToString(),
                        StringComparison.Ordinal
                    )
                )
                {
                    Directory.CreateDirectory(outputPath);
                    continue;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
                using (Stream input = entry.Open())
                using (
                    FileStream output = File.Open(
                        outputPath,
                        FileMode.Create,
                        FileAccess.Write,
                        FileShare.None
                    )
                )
                {
                    input.CopyTo(output);
                }
            }
        }

        public void Dispose()
        {
            archive.Dispose();
            executable.Dispose();
            if (File.Exists(temporaryZip))
            {
                File.Delete(temporaryZip);
            }
        }

        private static void CopyExactly(Stream input, Stream output, long bytes)
        {
            byte[] buffer = new byte[1024 * 1024];
            long remaining = bytes;
            while (remaining > 0)
            {
                int read = input.Read(
                    buffer,
                    0,
                    (int)Math.Min(buffer.Length, remaining)
                );
                if (read <= 0)
                {
                    throw new EndOfStreamException("Installer payload ended early.");
                }
                output.Write(buffer, 0, read);
                remaining -= read;
            }
        }
    }
}
