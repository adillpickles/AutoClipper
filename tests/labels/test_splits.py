import pytest

from autoclipper.labels.splits import make_folds, split_for, validate_version


def test_each_fold_is_disjoint_3_1_1_and_every_streamer_rotates() -> None:
    ids = [f"b{i}" for i in range(5)]
    folds = make_folds(ids, seed=42)
    for fold in folds:
        train, val, test = (set(fold[key]) for key in ("train", "val", "test"))
        assert (len(train), len(val), len(test)) == (3, 1, 1)
        assert not train & val
        assert not train & test
        assert not val & test
        assert train | val | test == set(ids)
        assert all(split_for(b, fold) == "train" for b in train)
    assert sorted(fold["test"][0] for fold in folds) == ids
    assert sorted(fold["val"][0] for fold in folds) == ids
    assert folds == make_folds(ids[::-1] + ids, seed=42)
    assert folds != make_folds(ids, seed=43)


@pytest.mark.parametrize("ids", [[], ["a", "b"], [str(i) for i in range(6)]])
def test_requires_five_streamers(ids: list[str]) -> None:
    with pytest.raises(ValueError, match="five streamers"):
        make_folds(ids, seed=0)


@pytest.mark.parametrize("version", ["../v1", ".", "nul", "CON.txt", "v1.", "a/b", ""])
def test_unsafe_version_is_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="portable"):
        validate_version(version)
