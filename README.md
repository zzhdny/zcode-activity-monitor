# ZCode 活动监视器

自动监视 ZCode 客户端里的限时活动（如"每人领取 3 亿 GLM-5.3-Flash Token"），
出现即弹 Windows 通知 + 响铃 + 闪烁任务栏提醒你领取。

## 工作原理（三层）

1. **实时检测（主力）**：每 20 秒用 Windows UI 自动化读取 ZCode 客户端窗口，
   只看**侧边栏左下角**（活动卡片出现的位置）的文字，主窗口之外还扫描
   ZCode 的悬浮卡片/弹窗（独立顶层窗口）。
   命中关键词规则（如"领取"、"Token"、"GLM-5.3-Flash"）且**连续 2 次扫描
   都命中**才提醒。
   防误报三道保险：任务列表项（ListItemControl）整棵排除、延伸出窗口底部的
   容器排除（列表容器高度会随内容变化）、主区域（聊天/编辑器）整棵剪枝。
   客户端**最小化或缩到托盘时照常检测**（已实测：隐藏状态下控件树仍可读，
   程序以侧边栏分隔条为参照系定位，不依赖窗口矩形，并缓存窗口句柄）。
2. **打开客户端兜底**：监视器随开机自启、常驻后台。就算客户端当时没开，
   你一打开客户端，只要活动卡片在，**30 秒内必定提醒**。
3. **定时兜底**：客户端关闭期间没有公开接口能查活动是否上线（已实测：
   官网是静态页、bigmodel 公告接口路径猜不中），所以按官方公告的轮次时间
   定时提醒，条目在 `config.json` 的 `schedules` 里增删。

## 文件

| 文件 | 作用 |
|---|---|
| `monitor.py` | 主程序 |
| `config.json` | 配置（关键词、检测区域、提醒策略、定时项） |
| `toast.ps1` | Windows 通知弹窗脚本 |
| `monitor.log` | 运行日志 |
| `state.json` | 提醒去重状态（自动生成） |

## 常用命令

```bat
py -3.12 monitor.py                :: 前台常驻监视（调试看输出用）
py -3.12 monitor.py --once         :: 只扫描一次，报告结果
py -3.12 monitor.py --test-toast   :: 测试通知+响铃+闪烁
py -3.12 monitor.py --install-startup  :: 写入开机自启（已执行过）
```

后台常驻方式（开机自启用同一条命令，已由启动文件夹里的
`zcode_activity_monitor.vbs` 自动完成）：

```bat
wscript "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\zcode_activity_monitor.vbs"
```

取消自启：删除上面那个 .vbs。停止当前常驻进程：任务管理器结束 `pythonw.exe`。

## 配置说明（config.json）

- `rules.any_of`：活动区域出现任一关键词就提醒，如 `"3亿"`、`"三亿"`。
- `rules.all_of`：两个词同时出现才提醒，如 `["活动", "领取"]`，用来降误报。
  活动文案变了（比如叫"福利"）就往这里加词。
- `zone`：检测区域 = 侧边栏 x 范围 × 窗口底部高度占比。客户端改版导致
  检测不到时，微调 `sidebar_max_fraction` / `bottom_fraction`。
- `alert.realert_minutes`：同一个活动重复提醒的最小间隔（默认 30 分钟）。
- `alert.max_alerts_per_day`：每天最多提醒几次（默认 8）。
- `schedules`：定时提醒，如
  `{"weekday": "fri", "time": "19:45", "message": "……"}`；
  weekday 取 mon/tue/wed/thu/fri/sat/sun。清空 `[]` 即关闭。
- `url_checks`：备用网页轮询，如
  `{"url": "https://...", "pattern": "3亿"}`；默认空。

## 已知限制

- 实时检测要求 ZCode 客户端**正在运行**：最小化、被其他窗口遮挡、缩到托盘都可以。
  只有客户端完全退出才无法检测，那时靠打开客户端后 30 秒内必提醒兜底
  （`schedules` 定时提醒也可作为补充，时间以官方公告为准）。
- 唯一的小盲区：客户端**已经**缩在托盘里时若监视器恰好（重新）启动，
  要等下次打开客户端一次才能"记住"窗口句柄，之后隐藏也没问题。
- 提醒只负责"叫你"，领取仍需在客户端内手动点击活动卡片。
- 依赖：Python 3.12 + `uiautomation`（已装好）。重装命令：
  `py -3.12 -m pip install uiautomation -i https://pypi.tuna.tsinghua.edu.cn/simple`
