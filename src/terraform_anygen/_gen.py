import anygen
import json
import os
import pathlib
import sys
import tinyshar
from . import _debugdump


def prefix_filter(d, prefix):
    result = {}
    prefix_len = len(prefix)

    for k, v in d.items():
        if k.startswith(prefix):
            result[k[prefix_len:]] = v

    return result


def main():
    query = json.load(sys.stdin)

    path = [pathlib.Path(i) for i in query["path"].split(os.pathsep)]

    age = anygen.AnygenEngine()
    ag = age.create(
        path=path,
        classes=query["classes"].split(",")
    )
    args = dict((k, json.loads(v)) for k, v in prefix_filter(query, "arg_").items())
    dump_path = query.get('debug_dump', None)

    if dump_path is not None:
        dump_path = pathlib.Path(dump_path)

    result = _debugdump.wrap(ag.produce, dump_path)(**args)

    for shar_key, shar_desc in result.pop("$shars", {}).items():
        def error(msg):
            sys.exit("$shars.%s: %s" % (shar_key, msg))

        if shar_key in result:
            error("already present in result")

        shar = tinyshar.SharCreator()

        def add_file(desc):
            dest = file_desc["destination"]
            dest_is_dir = dest.endswith('/') or dest.endswith('/.') or dest in ('', '.')
            dest = pathlib.PurePosixPath(dest)
            if sum(i in file_desc for i in ("content", "source")) != 1:
                error("exactly one of 'content' or 'source' must be present")

            if "content" in file_desc:
                content = file_desc["content"]
                if dest_is_dir:
                    if not isinstance(content, anygen.TemplateResultStr):
                        error("'content' must be result of named template expansion if 'destination' is a directory")

                    source_name = pathlib.PurePosixPath(content.name).name
            elif "source" in file_desc:
                source = path[0] / file_desc["source"]
                content = lambda: source.open("rb")  # noqa: E731
                if dest_is_dir:
                    source_name = source.name

            if dest_is_dir:
                dest = dest / source_name

            tags = file_desc.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]

            shar.add_file(dest.as_posix(), content, tags=tags)

        for file_desc in shar_desc.pop("files", []):
            add_file(file_desc)

        for chmod_tag, chmod_mode in shar_desc.pop("chmod", {}).items():
            tagged_files = shar.files_by_tag_as_shell_str(chmod_tag)
            if tagged_files:
                shar.add_post("chmod %s %s" % (chmod_mode, tagged_files))

        for i in shar_desc.pop("pre", []):
            shar.add_pre(i)

        for i in shar_desc.pop("post", []):
            shar.add_post(i)

        shebang = shar_desc.pop("shebang", '/bin/sh')

        if shar_desc:
            error("unparsed data : %s" % (shar_desc))

        result[shar_key] = b''.join(shar.render(
            shebang=shebang
        )).decode("latin-1")

    json.dump(
        result,
        sys.stdout
    )


try:
    main()
except KeyboardInterrupt:
    sys.exit(2)
