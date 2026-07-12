import pytest

from app.security import is_dangerous


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/x",
        "rm -r -f /tmp/x",
        "rm -fr /tmp/x",
        "rm --recursive --force /tmp/x",
        "rm  -rf   /tmp/x",  # 多重空白
        "cd /tmp && rm -rf x",  # 在 && 之後
        "rm -rf /tmp/x; echo done",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sdb1",
        "mkfs /dev/sdb1",
        "shutdown -h now",
        "reboot",
        "echo hi > /dev/sda",
        "userdel someuser",
    ],
)
def test_dangerous_commands_are_blocked(command):
    dangerous, reason = is_dangerous(command)
    assert dangerous is True
    assert reason


@pytest.mark.parametrize(
    "command",
    [
        "sleep 60",
        "python train.py --epochs 10",
        "rm file.txt",  # 沒有 -rf
        "rm -f file.txt",  # 只有 force 沒有 recursive
        "rm -r some_empty_dir",  # 只有 recursive 沒有 force
        "ls -la /tmp",
        "git pull",
        "rsync -a /data /backup",
    ],
)
def test_safe_commands_are_allowed(command):
    dangerous, reason = is_dangerous(command)
    assert dangerous is False
    assert reason == ""


def test_naive_blacklist_over_blocks_by_design():
    """設計選擇（見 app/security.py docstring）：這是純文字掃描，不解析
    shell 語法，所以像下面這種「文字上出現 rm -rf 但實際上不會刪檔」的
    指令，也會被判定為危險而擋下。這是刻意的取捨（寧可誤殺、不要漏放），
    這個測試把該行為明文釘住，避免日後不小心改壞。
    """
    dangerous, _ = is_dangerous("echo 'rm -rf /tmp/x'")
    assert dangerous is True

    dangerous, _ = is_dangerous("grep 'rm -rf' history.log")
    assert dangerous is True
