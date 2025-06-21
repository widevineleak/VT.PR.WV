import json
import pathlib
import shutil

from ruamel.yaml import YAML


# We can't subclass pathlib.Path directly (https://bugs.python.org/issue24132)
class Path(type(pathlib.Path())):
    def append_bytes(self, data, mode="ab", encoding="utf-8"):
        with self.open(mode, encoding=encoding) as fd:
            return fd.write(data)

    def append_line(self, line, **kwargs):
        return self.append_text(f"{line}\n")

    def append_text(self, text, mode="a", encoding="utf-8"):
        with self.open(mode, encoding=encoding) as fd:
            return fd.write(text)

    def format(self, **kwargs):
        return Path(str(self).format(**kwargs))

    def mkdirp(self):
        return self.mkdir(parents=True, exist_ok=True)

    def move(self, target):
        return Path(shutil.move(self, target))

    def open(self, mode="r", encoding=None, **kwargs):
        if not encoding and "b" not in mode:
            encoding = "utf-8"
        return super().open(mode, encoding=encoding, **kwargs)

    def read_json(self, missing_ok=False):
        try:
            with self.open() as fd:
                return json.load(fd)
        except FileNotFoundError:
            if missing_ok:
                return {}
            raise

    def read_text(self, encoding="utf-8"):
        return super().read_text(encoding=encoding)

    def read_yaml(self, missing_ok=False):
        try:
            return YAML().load(self.with_suffix(".yaml"))
        except FileNotFoundError:
            try:
                return YAML().load(self.with_suffix(".yml"))
            except FileNotFoundError:
                if missing_ok:
                    return {}
                raise

    def rmdir(self, missing_ok=False):
        try:
            super().rmdir()
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmtree(self, missing_ok=False):
        try:
            return shutil.rmtree(self)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def write_json(self, obj):
        with self.open("w") as fd:
            return json.dump(obj, fd)

    def write_text(self, text, encoding="utf-8"):
        return super().write_text(text, encoding=encoding)
