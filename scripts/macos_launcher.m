// Keep the GUI in a native app process so macOS can attribute file access to it.
#import <Cocoa/Cocoa.h>
#include <dlfcn.h>
#include <sys/stat.h>
#include <unistd.h>

static int fail(NSString *message, BOOL selfTest) {
    fprintf(stderr, "%s\n", message.UTF8String);
    if (!selfTest) {
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"iCourse 无法启动";
        alert.informativeText = [message stringByAppendingString:@"\n请重新运行 安装 Mac.command 修复运行环境。"];
        [alert runModal];
    }
    return 1;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        BOOL selfTest = argc == 2 && strcmp(argv[1], "--self-test") == 0;
        umask(0077);
        NSBundle *bundle = [NSBundle mainBundle];
        NSDictionary *runtime = [NSDictionary dictionaryWithContentsOfURL:
            [bundle URLForResource:@"runtime" withExtension:@"plist"]];
        NSString *python = runtime[@"PythonExecutable"];
        NSString *library = runtime[@"PythonLibrary"];
        if (![python isKindOfClass:[NSString class]] || !python.isAbsolutePath ||
            ![library isKindOfClass:[NSString class]] || !library.isAbsolutePath) {
            return fail(@"App 的运行环境配置缺失。", selfTest);
        }
        if (chdir(NSHomeDirectory().fileSystemRepresentation) != 0) {
            return fail(@"无法打开用户文件夹。", selfTest);
        }
        setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", 1);
        if (!selfTest) {
            NSString *logs = [NSHomeDirectory() stringByAppendingPathComponent:
                @"Library/Logs/Fudan iCourse Subscriber"];
            NSError *error = nil;
            if (![[NSFileManager defaultManager] createDirectoryAtPath:logs
                    withIntermediateDirectories:YES attributes:nil error:&error]) {
                return fail(@"无法创建 App 日志目录。", NO);
            }
            NSString *log = [logs stringByAppendingPathComponent:@"application.log"];
            if (!freopen(log.fileSystemRepresentation, "a", stdout) || dup2(fileno(stdout), STDERR_FILENO) < 0) {
                return fail(@"无法打开 App 日志。", NO);
            }
            setvbuf(stdout, NULL, _IONBF, 0);
        }
        // Do not exec Python: replacing this executable loses the native app identity.
        void *handle = dlopen(library.fileSystemRepresentation, RTLD_NOW | RTLD_GLOBAL);
        if (!handle) {
            fprintf(stderr, "dlopen: %s\n", dlerror());
            return fail(@"无法载入 Python 运行环境。", selfTest);
        }
        int (*pythonMain)(int, char **) = dlsym(handle, "Py_BytesMain");
        if (!pythonMain) {
            return fail(@"Python 运行环境缺少启动接口。", selfTest);
        }
        // argv[0] selects the installed venv and remains sys.executable for workers.
        // -I ignores the shell's Python path and user site-packages.
        char *pythonArgs[] = {
            (char *)python.fileSystemRepresentation, "-I", "-u", "-m",
            selfTest ? "src.mac_app_check" : "src.mac_gui",
            (char *)(bundle.bundleIdentifier ?: @"").UTF8String, NULL
        };
        // Normal Finder arguments are never interpreted as Python options/code.
        return pythonMain(selfTest ? 6 : 5, pythonArgs);
    }
}
