from app.monitor import (
    GpuReading,
    is_idle,
    parse_df_output,
    parse_full_probe_output,
    parse_loadavg,
    parse_nvidia_smi,
    parse_probe_output,
)


# ---------------------------------------------------------------------------
# parse_nvidia_smi
# ---------------------------------------------------------------------------


def test_parse_nvidia_smi_single_gpu():
    text = "12, 1024, 8192\n"
    readings = parse_nvidia_smi(text)
    assert readings == [GpuReading(util_percent=12.0, mem_used_mb=1024.0, mem_total_mb=8192.0)]


def test_parse_nvidia_smi_multi_gpu():
    text = "5, 100, 8192\n90, 7000, 8192\n0, 0, 24576\n"
    readings = parse_nvidia_smi(text)
    assert len(readings) == 3
    assert readings[1].util_percent == 90.0
    assert readings[2].mem_total_mb == 24576.0


def test_parse_nvidia_smi_empty_output():
    assert parse_nvidia_smi("") == []
    assert parse_nvidia_smi("\n\n") == []


def test_parse_nvidia_smi_garbled_lines_are_skipped_not_fatal():
    text = "not,a,valid,line\n12, 1024, 8192\ngarbage without commas\n"
    readings = parse_nvidia_smi(text)
    # 只有中間那行是合法的三欄位數字
    assert len(readings) == 1
    assert readings[0].util_percent == 12.0


def test_parse_nvidia_smi_non_numeric_values_skipped():
    text = "abc, def, ghi\n33, 500, 8192\n"
    readings = parse_nvidia_smi(text)
    assert len(readings) == 1
    assert readings[0].util_percent == 33.0


# ---------------------------------------------------------------------------
# parse_loadavg
# ---------------------------------------------------------------------------


def test_parse_loadavg_normal():
    assert parse_loadavg("0.12 0.34 0.56 1/234 5678\n") == 0.12


def test_parse_loadavg_empty():
    assert parse_loadavg("") is None
    assert parse_loadavg("   \n") is None


def test_parse_loadavg_garbled():
    assert parse_loadavg("not a loadavg line at all\n") is None


def test_parse_probe_output_combines_both():
    text = "10, 200, 8192\n---LOADAVG---\n0.5 0.4 0.3 2/300 999\n"
    gpus, load1 = parse_probe_output(text)
    assert len(gpus) == 1
    assert gpus[0].util_percent == 10.0
    assert load1 == 0.5


# ---------------------------------------------------------------------------
# parse_df_output（階段 3：磁碟餘量顯示、sync 前空間檢查共用）
# ---------------------------------------------------------------------------


def test_parse_df_output_normal():
    text = "Filesystem     1024-blocks      Used Available Capacity Mounted on\n/dev/sda1 100000000 1000000 99000000 2% /\n"
    assert parse_df_output(text) == 99000000 * 1024


def test_parse_df_output_empty_or_garbled():
    assert parse_df_output("") is None
    assert parse_df_output("garbage\n") is None
    assert parse_df_output("only three fields here\n") is None


# ---------------------------------------------------------------------------
# parse_full_probe_output（階段 3：拆 GPU / loadavg / df 三段）
# ---------------------------------------------------------------------------


def test_parse_full_probe_output_with_df_section():
    text = (
        "10, 200, 8192\n---LOADAVG---\n0.5 0.4 0.3 2/300 999\n"
        "---DF---\n/dev/sda1 100000000 1000000 99000000 2% /\n"
    )
    gpus, load1, disk_avail = parse_full_probe_output(text)
    assert len(gpus) == 1
    assert load1 == 0.5
    assert disk_avail == 99000000 * 1024


def test_parse_full_probe_output_without_df_section_is_backward_compatible():
    """沒有 ---DF--- 區段（例如舊格式）時，disk_avail 是 None，gpu/load1
    解析結果跟階段 1 的 parse_probe_output() 完全一致。"""
    text = "10, 200, 8192\n---LOADAVG---\n0.5 0.4 0.3 2/300 999\n"
    gpus, load1, disk_avail = parse_full_probe_output(text)
    assert len(gpus) == 1
    assert load1 == 0.5
    assert disk_avail is None


# ---------------------------------------------------------------------------
# is_idle
# ---------------------------------------------------------------------------


def test_is_idle_offline_is_never_idle():
    assert is_idle(
        online=False,
        has_running_job=False,
        is_gpu_server=True,
        gpu_util_max=0.0,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is False


def test_is_idle_has_running_job_is_not_idle():
    assert is_idle(
        online=True,
        has_running_job=True,
        is_gpu_server=True,
        gpu_util_max=0.0,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is False


def test_is_idle_gpu_server_below_threshold_is_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=True,
        gpu_util_max=5.0,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is True


def test_is_idle_gpu_server_above_threshold_is_not_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=True,
        gpu_util_max=20.0,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is False


def test_is_idle_gpu_server_unknown_util_is_conservatively_not_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=True,
        gpu_util_max=None,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is False


def test_is_idle_cpu_server_below_threshold_is_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=False,
        gpu_util_max=None,
        idle_gpu_util=15.0,
        load1=1.0,
        idle_load=2.0,
    ) is True


def test_is_idle_cpu_server_above_threshold_is_not_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=False,
        gpu_util_max=None,
        idle_gpu_util=15.0,
        load1=3.0,
        idle_load=2.0,
    ) is False


def test_is_idle_cpu_server_unknown_load_is_conservatively_not_idle():
    assert is_idle(
        online=True,
        has_running_job=False,
        is_gpu_server=False,
        gpu_util_max=None,
        idle_gpu_util=15.0,
        load1=None,
        idle_load=2.0,
    ) is False
