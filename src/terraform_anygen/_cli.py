import ansimarkup
import anygen
import attrdict
import click
import json
import os
import pathlib
import python_terraform
import ruamel.yaml
import shutil
import sys
from . import _debugdump


def checked_tf(tf_result, what):
    if tf_result[0] != 0:
        raise click.ClickException("'terraform %s' failed with return code %d" % (what, tf_result[0]))


def checked_captured_tf(tf_result, what):
    if tf_result[0] != 0:
        click.echo(tf_result[1], err=True)
        click.echo(tf_result[2], err=True)
        raise click.ClickException("'terraform %s' failed with return code %d" % (what, tf_result[0]))


def dump_json(path, data):
    with path.open(mode='w') as f:
        json.dump(data, f, indent=4, sort_keys=True)


class InputParamType(click.ParamType):
    name = 'input'

    def convert(self, value, param, ctx):
        if value.startswith('@'):
            value = value[1:]
            if value.endswith('.json'):
                with open(value, 'r') as f:
                    return json.load(f)

            if value.endswith(('.yml', '.yaml')):
                with open(value) as f:
                    return ruamel.yaml.safe_load(f)

            self.fail("don't know how to read %s" % value)

        sp = value.split('=', 1)
        if len(sp) == 1:
            return {value: True}

        try:
            return {sp[0]: json.loads(sp[1])}
        except json.JSONDecodeError:
            return {sp[0]: sp[1]}


@click.command()
@click.option('--yes', '-y', is_flag=True)
@click.option('--jobs', '-j', type=int, default=10)
@click.option('--state', type=click.Path(file_okay=False))
@click.option('--target', multiple=True)
@click.option('--destroy', '-d', is_flag=True)
@click.option('--model', type=click.Path(file_okay=False, exists=True))
@click.option('--classes')
@click.option('--def', '-D', multiple=True, type=InputParamType())
@click.option('--refresh/--no-refresh', default=True)
@click.option('--force-backend-copy/--no-force-backend-copy', default=True)
def main(**kwargs):
    if shutil.which('terraform') is None:
        raise click.ClickException("cannot find 'terraform' executable on PATH")

    opts = attrdict.AttrMap(kwargs)

    state_dir = pathlib.Path(opts.state or '.terraform-anygen')
    terraform_dir = state_dir / 'terraform'
    out_dir = state_dir / 'out'

    def rmtree_out_dir():
        if out_dir.exists():
            shutil.rmtree(str(out_dir))

    if not state_dir.exists():
        terraform_dir.mkdir(parents=True)

    debug_dir = state_dir / 'debug'
    if debug_dir.exists():
        shutil.rmtree(str(debug_dir))
    debug_dir.mkdir()

    terraform = python_terraform.Terraform(
        working_dir=terraform_dir,
        targets=['module.body.' + i for i in opts.target]
    )

    if not opts.destroy:
        rmtree_out_dir()

    model_dir = pathlib.Path(opts.model or '.')
    path = [model_dir]

    age = anygen.AnygenEngine()
    ag = age.create(
        path=path,
        classes=[
            'terraform' + i
            for i in (['.' + j for j in opts.classes.split(',')] if opts.classes else [''])
        ]
    )
    ag_result = _debugdump.wrap(ag.produce, debug_dir / 'terraform')(*opts["def"])
    tf_data = attrdict.AttrDict()
    tf_data += ag_result.get("terraform", {})

    if opts.destroy:
        tf_data = {"provider": tf_data.get("provider", [])}
    else:
        data_external = {}
        path_str = os.pathsep.join(str(i.resolve()) for i in path)

        for k, v in ag_result.get("anygen", {}).items():
            query = {
                "path": path_str,
                "classes": v["classes"],
                "debug_dump": str(debug_dir.joinpath('anygen.' + k).absolute())
            }
            for k2, v2 in v.get("args", {}).items():
                query["arg_" + k2] = '${jsonencode("%s")}' % v2.replace('\\', '\\\\').replace('"', '\\"')

            data_external[k] = {
                "program": [sys.executable, "-m", "terraform_anygen._gen"],
                "query": query
            }

        if data_external:
            tf_data += {"data": {"external": data_external}}

        output = ag_result.get("output", None)
        if output:
            tf_data += {"output": dict((k, dict(value=v)) for k, v in output.items())}

    main_tf_data = attrdict.AttrDict()
    main_tf_data += {"module": {"body": {"source": "./body"}}}

    tf_backend = ag_result.get("backend", None)
    if tf_backend is not None:
        main_tf_data += {"terraform": {"backend": tf_backend}}

    dump_json(terraform_dir.joinpath("main.tf.json"), main_tf_data)

    body_module_dir = terraform_dir / "body"
    body_module_dir.mkdir(exist_ok=True)

    dump_json(body_module_dir.joinpath("main.tf.json"), tf_data)

    checked_tf(
        terraform.init(
            capture_output=False,
            force_copy=opts.force_backend_copy
        ),
        'init'
    )

    if opts.destroy:
        checked_tf(
            terraform.destroy(
                capture_output=False,
                force=opts.yes,
                parallelism=opts.jobs,
                no_color=python_terraform.IsNotFlagged,
                refresh=opts.refresh
            ),
            'destroy'
        )

        rmtree_out_dir()
    else:
        checked_tf(
            terraform.apply(
                capture_output=False,
                skip_plan=opts.yes,
                parallelism=opts.jobs,
                refresh=opts.refresh,
                no_color=python_terraform.IsNotFlagged
            ),
            'apply'
        )

        on_sucess_classes = ag_result.get("on_success", {}).get("classes", [])
        if on_sucess_classes:
            tf_result = terraform.cmd(
                'state pull',
                capture_output=True,
            )
            checked_captured_tf(tf_result, 'state pull')
            tfstate = json.loads(tf_result[1])

            expected_outfiles = set()

            def jinjafilter_outfile(name):
                expected_outfiles.add(name)
                return out_dir.joinpath(name).resolve()

            ag_success = age.create(
                path=path,
                classes=on_sucess_classes,
                extras=dict(
                    jinjafilter=dict(
                        outfile=jinjafilter_outfile
                    )
                )
            )

            for i in tfstate["modules"]:
                if i["path"] == ["root", "body"]:
                    body_module_outputs = i["outputs"]
                    break
            else:
                raise click.ClickException("failed to find 'body' module in terraform state")

            body_module_outputs = dict((k, v["value"]) for k, v in body_module_outputs.items())

            ag_success_result = \
                _debugdump.wrap(ag_success.produce, debug_dir / 'on_success')(
                    outputs=body_module_outputs
                )
            out_dir.mkdir()
            for k, v in ag_success_result.get("files", {}).items():
                if isinstance(v, str):
                    v = dict(content=v)

                expected_outfiles.discard(k)

                rel_path = pathlib.Path(k)
                if rel_path.is_absolute() or '..' in rel_path.parts or not rel_path.parts:
                    raise click.ClickException(
                        "output file path cannot be absolute, contain '..', or be empty: {}".format(rel_path)
                    )

                file_path = out_dir / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(v["content"])

                file_mode = v.get("chmod", None)
                if file_mode is not None:
                    file_path.chmod(file_mode)

            if expected_outfiles:
                raise click.ClickException(
                    "the following files have been referenced via 'outfile' but not produced: "
                    + ', '.join(sorted(expected_outfiles))
                )

            text = ag_success_result.get("text", None)
            if text:
                if not ag_success_result.get("plaintext", False):
                    text = ansimarkup.parse(text)
                click.echo(text)
