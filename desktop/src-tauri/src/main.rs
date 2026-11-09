#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! 凌扬运动 LYNVOS FIT · 桌面壳
//!
//! 职责只有三件：把 Python 内核拉起来、等它就绪后开窗、退出时把它带走。
//! 界面与全部 API 仍由 web/index.html + web/server.py 提供，一行前端代码都不改。
//!
//! 生命周期契约（用户明确关心「关掉窗口会不会留后台服务」）：
//!   - 启动：读 %LOCALAPPDATA%\LynvosFit\port.json，若端口已活则复用已有实例，
//!     不重复拉起内核；否则 spawn sidecar 并等待 port.json 出现。
//!   - 就绪：WebviewWindow 指向 http://127.0.0.1:<port>/（External URL）。
//!   - 退出：RunEvent::Exit 时 kill + wait 子进程；sidecar 另有 --parent-pid
//!     守护线程，父进程被强杀也会自行 os._exit(0)，双保险。
//!   - 启动失败/超时：立刻 kill 已 spawn 的子进程，不允许留下孤儿服务。

use std::{
    env, fs,
    net::{TcpStream, ToSocketAddrs},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

/// None = 复用了别的实例的内核，本进程不负责杀它。
struct Sidecar(Mutex<Option<Child>>);

fn local_home() -> PathBuf {
    if let Ok(v) = env::var("LYNVOS_HOME") {
        if !v.trim().is_empty() {
            return PathBuf::from(v);
        }
    }
    let base = env::var("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            env::var("USERPROFILE")
                .map(|p| PathBuf::from(p).join("AppData").join("Local"))
                .unwrap_or_else(|_| PathBuf::from("."))
        });
    base.join("LynvosFit")
}

fn sidecar_path(resource_dir: &Path) -> Option<PathBuf> {
    if let Ok(p) = env::var("LYNVOS_SIDECAR") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    // 第一个是正式安装包路径；后面两个是 cargo run / 手工调试的回退。
    let candidates = [
        resource_dir
            .join("resources")
            .join("lynvos")
            .join("lynvos-sidecar.exe"),
        resource_dir.join("lynvos-sidecar.exe"),
        resource_dir
            .join("..")
            .join("..")
            .join("..")
            .join("dist")
            .join("sidecar")
            .join("lynvos-sidecar.exe"),
    ];
    candidates.iter().find(|p| p.is_file()).cloned()
}

fn port_alive(port: u16) -> bool {
    match ("127.0.0.1", port).to_socket_addrs() {
        Ok(mut it) => match it.next() {
            Some(addr) => TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok(),
            None => false,
        },
        Err(_) => false,
    }
}

/// port.json 存在且端口确实在监听，才认为是可复用的活实例（避免读到上次退出残留）。
fn endpoint(home: &Path) -> Option<u16> {
    let text = fs::read_to_string(home.join("port.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let port = value.get("port")?.as_u64()? as u16;
    if port_alive(port) {
        Some(port)
    } else {
        None
    }
}

fn wait_endpoint(home: &Path, timeout_secs: u64) -> Result<u16, Box<dyn std::error::Error>> {
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(timeout_secs) {
        if let Some(port) = endpoint(home) {
            return Ok(port);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!(
        "后端在 {} 秒内未就绪，请检查 {}/port.json 与本机 127.0.0.1 回环是否可用",
        timeout_secs,
        home.display()
    )
    .into())
}

fn kill_child(handle: &tauri::AppHandle) {
    if let Some(state) = handle.try_state::<Sidecar>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| -> Result<(), Box<dyn std::error::Error>> {
            let home = local_home();
            fs::create_dir_all(&home)?;
            let resource_dir = app.path().resource_dir()?;

            let port = if let Some(p) = endpoint(&home) {
                // 已有实例在跑：只开窗，不拉起、也不负责结束别人的内核。
                app.manage(Sidecar(Mutex::new(None)));
                p
            } else {
                let exe = sidecar_path(&resource_dir).ok_or_else(|| {
                    format!(
                        "未找到 Python 内核 lynvos-sidecar.exe（查找目录：{}）。\
                         请先运行 desktop/build.py 构建 sidecar。",
                        resource_dir.display()
                    )
                })?;
                let child = Command::new(&exe)
                    .env("LYNVOS_HOME", &home)
                    .env("LYNVOS_PARENT_PID", std::process::id().to_string())
                    .env("LYNVOS_PORT", "8787")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()?;
                app.manage(Sidecar(Mutex::new(Some(child))));
                // 关键：等待失败也必须收掉子进程，否则升级/端口冲突会留下孤儿服务。
                match wait_endpoint(&home, 45) {
                    Ok(p) => p,
                    Err(e) => {
                        kill_child(app.handle());
                        return Err(e);
                    }
                }
            };

            // 不显式写 url::Url 类型：由 WebviewUrl::External 的变体类型推断，
            // 避免为了一个 URL 解析额外引入 url crate 依赖。
            let url = format!("http://127.0.0.1:{}/", port);
            let target = WebviewUrl::External(url.parse()?);
            WebviewWindowBuilder::new(app, "main", target)
                .title("凌扬运动 LYNVOS FIT · 自媒体工作台")
                .inner_size(1440.0, 900.0)
                .min_inner_size(1080.0, 700.0)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动凌扬运动工作台失败");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit) {
            kill_child(handle);
        }
    });
}
