# The MT4 "32 terminals per Windows user" limit: where it lives, and how to get past it

If you run a MetaTrader farm you will eventually hit a wall: a single Windows
user session refuses to run more than **32** MT4 terminals at once. The 33rd just
doesn't come up. This is folklore on the forums, usually hand-waved as "a mutex."

This note is the result of actually pinning it down — reproducing the cap on a
clean box, then locating the exact kernel object that enforces it and explaining
*why* it is scoped the way it is. It also explains why `mt4ctl` exists: running MT4
headless under Wine on Linux sidesteps this limit entirely, because the limit is a
property of the **Windows session object namespace**, not of MetaTrader's licensing
or of the hardware.

> **TL;DR.** Each running terminal registers a named **Mutant** (mutex) in the
> *session-local* object directory `\Sessions\<id>\BaseNamedObjects`. The cap is the
> number of those objects in that directory. Because the directory is per-session,
> the limit is per-session/per-user — so a second Windows session gives you another
> 32. Wine on Linux does **not** remove the cap: it reimplements the same namespace
> per **wineprefix**, so the 32-cap reappears inside one prefix — but each separate
> wineprefix gets its own 32 (both verified below). RAM and CPU are nowhere near the
> bottleneck.

---

## 1. Reproduce the cap

The test host: Windows 11 Pro for Workstations, 192 logical cores, 256 GB RAM,
MT4 build `4.0.0.1470`. The console session was clean of any native MT4 before the
test, so the count would be unambiguous.

To probe a *count* limit you don't need real trading. You need lots of independent
terminal processes. So: take one clean MT4 install, make 33 **stripped, portable**
copies (no experts, no saved account → no broker connections, no tick history),
and launch them all in the interactive session.

```powershell
# stripped template: terminal.exe + config, minus history/EAs/logs/account
robocopy $src $tmpl /E /R:0 /W:0 /XD history tester logs profiles MQL4\Experts experts
Remove-Item $tmpl\config\accounts.ini -EA SilentlyContinue   # no auto-login
# fan out to t01..t33, each launched as: terminal.exe /portable
```

GUI MT4 cannot be launched from an SSH/service context — it needs a real desktop —
so the launcher runs via a scheduled task with `/IT` (interactive), which executes
in the logged-on session.

**Result:**

| metric | value |
| --- | --- |
| launched | 33 |
| processes alive (each wrote a log, ~107 MB RAM) | **32** |
| 33rd terminal | **no process, no log — never started** |
| total RAM for 32 | ~3.4 GB (of 256 GB) |
| Application/System event log: desktop-heap / out-of-memory / crash | **none** |

The cap is exactly 32, and it is a **hard application-side limit**, not GUI-resource
exhaustion: 32 × 107 MB is trivial on this box, and Windows logged no desktop-heap
or out-of-resource events. The 33rd process simply refuses to initialize.

(One measurement gotcha: `Process.MainWindowHandle` read from a service/SSH session
returns `0` for all of them, because `EnumWindows` from session 0 can't see another
session's desktop. The terminals are genuinely up — their logs and RAM prove it.)

---

## 2. Find the lock — without Sysinternals

The classic move is `handle.exe -p terminal.exe` (Sysinternals) or the
`NtObjectManager` PowerShell module. Neither was installed, and pulling binaries
onto a production trading box is a non-starter. But you don't need them — the
Windows object-manager namespace is enumerable directly through two `ntdll` calls,
`NtOpenDirectoryObject` + `NtQueryDirectoryObject`, reachable from PowerShell via
P/Invoke:

```powershell
$cs = @'
using System; using System.Runtime.InteropServices; using System.Text;
public static class ObjDir {
  [StructLayout(LayoutKind.Sequential)] struct UNICODE_STRING { public ushort Length, MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)] struct OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory, ObjectName; public uint Attributes; public IntPtr SecurityDescriptor, SQOS; }
  [StructLayout(LayoutKind.Sequential)] struct OBJDIR_INFO { public UNICODE_STRING Name, TypeName; }
  [DllImport("ntdll.dll")] static extern int NtOpenDirectoryObject(out IntPtr h, uint a, ref OBJECT_ATTRIBUTES oa);
  [DllImport("ntdll.dll")] static extern int NtQueryDirectoryObject(IntPtr h, IntPtr buf, int len, bool one, bool restart, ref uint ctx, out uint ret);
  public static string List(string path) {
    var sb = new StringBuilder();
    var us = new UNICODE_STRING { Buffer = Marshal.StringToHGlobalUni(path),
      Length = (ushort)(path.Length*2), MaximumLength = (ushort)(path.Length*2+2) };
    var usp = Marshal.AllocHGlobal(Marshal.SizeOf(us)); Marshal.StructureToPtr(us, usp, false);
    var oa = new OBJECT_ATTRIBUTES { Length = Marshal.SizeOf(typeof(OBJECT_ATTRIBUTES)), ObjectName = usp, Attributes = 0x40 };
    IntPtr dir; if (NtOpenDirectoryObject(out dir, 1, ref oa) != 0) return "OPEN_FAILED";
    var buf = Marshal.AllocHGlobal(8192); uint ctx = 0; bool restart = true;
    while (true) {
      int st = NtQueryDirectoryObject(dir, buf, 8192, true, restart, ref ctx, out _); restart = false;
      if (st != 0 && st != 0x105) break;                       // 0x105 = STATUS_MORE_ENTRIES
      var i = (OBJDIR_INFO)Marshal.PtrToStructure(buf, typeof(OBJDIR_INFO));
      if (i.Name.Buffer == IntPtr.Zero) break;
      sb.AppendLine(Marshal.PtrToStringUni(i.TypeName.Buffer, i.TypeName.Length/2)
        + "\t" + Marshal.PtrToStringUni(i.Name.Buffer, i.Name.Length/2));
    }
    return sb.ToString();
  }
}
'@
Add-Type -TypeDefinition $cs | Out-Null
[ObjDir]::List('\Sessions\1\BaseNamedObjects')   # Mutant / Semaphore / Section / Event …
```

The method: snapshot the named objects with **zero** terminals, snapshot again with
a few running, and diff. A "max N instances" gate is usually a named **semaphore**
(count) or a set of slot objects — so the *type* of the new object would tell us the
mechanism. The signal, after filtering out the per-PID noise (`WilError`/`WilStaging`
Windows Error Reporting objects, `HWNDInterface` UI sections, .NET `Cor_*` blocks):

```
\Sessions\1\BaseNamedObjects   Mutant   DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDU=
\Sessions\1\BaseNamedObjects   Mutant   DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDY=
```

Named **Mutant** objects, in the **session-local** `BaseNamedObjects`, with
base64-looking names sharing a long constant prefix.

---

## 3. What the lock actually is

Decode the base64 names to bytes and they are identical except for the **last
byte(s)**:

```
DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDC=  ->  0ca0…29314 30
DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDQ=  ->  0ca0…29314 34
DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDU=  ->  0ca0…29314 35
DKDSY1CRYOKBSHBAW0LMTOD/EAKTFDY=  ->  0ca0…29314 36
```

So the name is `base64( <constant 22-byte blob> + <short per-instance suffix> )`. The
22-byte prefix (its base64 form starts `DKDSY1CRYOKBSHBAW0LMTOD/EAKTFD…`; the bytes
are machine-specific — yours will differ) is a per-install/machine identifier,
**constant for every instance on the box**. The suffix distinguishes the running
instances.

A first, tidy hypothesis — "the last byte is an ASCII slot index, `0x30 + slot`" —
fit the first four samples beautifully. It was wrong. Pushed to the cap, the
suffixes scatter and grow to two bytes:

```
count = 32   suffix lengths = {1, 2}   sample suffixes: 30, 34, 35, 3540, 3551, 3d, 30d5 …
```

So the per-instance part is a short variable-length token, not a clean `0..31`
index. Fully reversing its encoding would need a disassembler; it isn't necessary to
answer the question. What the controlled experiments *do* establish:

- **1 instance → 1 mutant.** And launching a single terminal from `t01` vs `t02`
  produces the **same** name — so the object is tied to the instance registration,
  **not** to the data-folder path. It is a shared instance registry, not a
  per-folder single-instance lock.
- **32 instances → 32 distinct mutants.** The 33rd cannot register and the process
  exits. The cap is the population of these prefix-named mutants in the session
  namespace.
- **The prefix is machine-derived, not path-derived** (constant across copies in
  different folders). So you cannot escape the cap by installing MT4 several times on
  one machine — every install on that machine shares the same registry.

There was no separate shared semaphore or counter section — with one instance
running, the *only* MT4-specific object was that single mutant. The count of these
mutants **is** the gate.

---

## 4. Why it's "per Windows user"

The directory is `\Sessions\<id>\BaseNamedObjects`, where `<id>` is the Terminal
Services session ID. The mutant is created **without** a `Global\` prefix, so it
lands in the caller's *session* namespace, not the global one. Each session has its
own, independent directory:

```
\Sessions\0\BaseNamedObjects      # services session
\Sessions\1\BaseNamedObjects      # the interactive console session  <- MT4's mutants here
\BaseNamedObjects                 # the Global\ namespace (shared across sessions)
```

That session-scoping *is* the limit's nature. It immediately tells you how to beat
it — and how not to.

---

## 5. How to actually scale past 32

**Wine on Linux — but mind the wineprefix.** The intuitive assumption is that Wine,
being a different OS, escapes the cap. It's subtler than that, and worth getting
right because it dictates how you lay out a Wine farm.

Wine reimplements the NT object manager in the **`wineserver`**, and there is *one
`wineserver` per `WINEPREFIX`*. All Wine processes sharing a prefix share that
server's named-object namespace (kernel objects, registry, shared memory). So
MetaTrader's instance registry is shared across every terminal in one prefix — and
the **32-cap reappears per wineprefix.**

Verified on a Linux/WSL2 host (Wine 9.0): 33 stripped terminals launched into a
**single** `WINEPREFIX` → **32 came up and wrote logs, the 33rd did not** — exactly
the Windows result. What Wine *does* change is the scope: the cap is now per
**wineprefix** instead of per Windows **session**, and separate prefixes are fully
independent `wineserver` instances. In the same test, an unrelated 7-terminal farm
in a different prefix kept running throughout — **39 terminals across two prefixes
on one box**, each prefix under its own 32.

So the rule for a Wine farm: **one wineprefix per terminal** (or per group of ≤32),
not one shared prefix. That's what makes Wine scale past 32 — not Wine itself.
(It's also, conveniently, the isolation layout most "run many MT4 on Linux" guides
already recommend, for unrelated config-conflict reasons.) This is the surface
`mt4ctl` drives: MT4 under `Wine + Xvfb + systemd`, one unit per terminal, over SSH —
with supervision, restart-on-failure, and headless operation. Past the per-prefix
32 the practical ceiling becomes RAM, the X display, and your broker's order limits.

**Add Windows sessions.** Each additional Terminal Services session gets its own
`\Sessions\N\BaseNamedObjects`, hence another 32. The honest details:

- A new interactive session requires a **real logon** (RDP or console). On client
  Windows (non-Server) only one session is *on the display* at a time, but a
  **disconnected** session keeps its processes running — fine for a headless farm.
- **`runas` / secondary logon does *not* help.** It creates a new logon token but
  the process stays in the *same* TS session, so its mutant lands in the same
  `\Sessions\<id>\BaseNamedObjects` — same 32.
- True *concurrent* on-screen sessions on client Windows need a `termsrv.dll`
  patch (RDP Wrapper) — invasive and update-fragile. A Windows VM is the clean
  alternative if you really need native sessions.

**Binary-patch `terminal.exe`.** You could NOP the count check. It's brittle (lost
on every auto-update), risky, and pointless in practice because of the next section.

---

## 6. The walls behind the wall

Defeating the 32-cap rarely helps, because other limits bite around the same place —
in *one* session:

- **Desktop heap.** The interactive desktop heap (`SharedSection` second value,
  commonly 20 MB) is shared by every GUI app in the session. Heavy MT4 instances
  (many charts) exhaust it well before hardware does; light ones fit more. The
  non-interactive desktop heap (third value, ~4 MB) is why GUI MT4 launched into
  session 0 dies outright.
- **GDI / USER handles** per process and system-wide.
- **MT4's single trade context.** All experts in a terminal share one trade
  context; at each bar open dozens of EAs fire at once, serialize, and the window
  appears to freeze for a beat. This caps *useful* EAs-per-terminal long before the
  100-charts hard limit.
- **Broker order caps** (e.g. a few hundred open+pending per account) — independent
  of how many terminals you run.

So "32 terminals" is rarely the real ceiling for a dense farm. Spreading EAs across
*more, lighter* terminals — and running them headless on Linux with **one wineprefix
per terminal**, so each lives in its own namespace — scales better than fighting a
single Windows session (or a single shared wineprefix).

---

## Takeaways

- The "32 MT4 per Windows user" limit is real and exact — verified 33 → 32 on a
  clean box, with no resource pressure.
- It is enforced by the count of per-instance **Mutant** objects in the
  **session-local** `\Sessions\<id>\BaseNamedObjects`, named
  `base64(<machine constant> + <per-instance token>)`.
- It is namespace-scoped: you get another 32 per additional Windows **session**, or
  per additional Wine **wineprefix**. Wine does not remove the cap — it makes extra
  namespaces cheap (more prefixes, no extra OS users or `termsrv` patch). Verified:
  33 → 32 inside one wineprefix; 39 terminals across two prefixes on one Linux box.
- You can enumerate the Windows object namespace with two `ntdll` calls; no
  Sysinternals required.
- Past 32 you meet desktop heap, GDI, the bar-open trade-context storm, and broker
  caps — design for many light terminals, not a few heavy ones.

*Methodology: stripped portable terminal copies launched into isolated namespaces
(an interactive scheduled task on Windows; a dedicated `WINEPREFIX` + `Xvfb` on
Wine); named-object enumeration via `NtQueryDirectoryObject` P/Invoke;
baseline-vs-N diffing; base64 decode of the mutant names. Native MT4 build
`4.0.0.1470`; Wine 9.0 on WSL2.*
