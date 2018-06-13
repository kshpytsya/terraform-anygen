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
from functools import update_wrapper
from . import _debugdump


def checked_tf(tf_result, what):
    if tf_result[0] != 0:
        raise click.ClickException("'terraform %s' failed with return code %d" % (what, tf_result[0]))


def checked_captured_tf(tf_result, what):
    if tf_result[0] != 0:
        click.echo(tf_result[1], err=True)
        click.echo(tf_result[2], err=True)
        raise click.ClickException("'terraform %s' failed with return code %d" % (what, tf_result[0]))


def opts_obj(f):
    @click.pass_context
    def wrapper(ctx, *args, **kwargs):
        return ctx.invoke(f, *(args + (attrdict.AttrMap(kwargs),)))
    return update_wrapper(wrapper, f)


@click.group()
@click.option('-y', is_flag=True)
@click.option('--state', type=click.Path(file_okay=False))
@click.pass_context
@opts_obj
def main(ctx, opts):
    if shutil.which('terraform') is None:
        raise click.ClickException("cannot find 'terraform' executable on PATH")

    ctx.obj = attrdict.AttrMap()
    ctx.obj.yes = opts.y
    ctx.obj.state_dir = pathlib.Path(opts.state or '.terraform-anygen')
    ctx.obj.terraform_dir = ctx.obj.state_dir / 'terraform'
    ctx.obj.out_dir = ctx.obj.state_dir / 'out'

    def rmtree_out_dir():
        if ctx.obj.out_dir.exists():
            shutil.rmtree(str(ctx.obj.out_dir))

    ctx.obj.rmtree_out_dir = rmtree_out_dir

    if not ctx.obj.state_dir.exists():
        ctx.obj.terraform_dir.mkdir(parents=True)

    ctx.obj.debug_dir = ctx.obj.state_dir / 'debug'
    if ctx.obj.debug_dir.exists():
        shutil.rmtree(str(ctx.obj.debug_dir))
    ctx.obj.debug_dir.mkdir()

    ctx.obj.terraform = python_terraform.Terraform(working_dir=ctx.obj.terraform_dir)


class InputParamType(click.ParamType):
    name = 'input'

    def convert(self, value, param, ctx):
        if value.startswith('@'):
            value = value[1:]
            if value.endswith('.json'):
                with open(value, 'r') as f:
                    return json.load(f)

            if value.endswith(('.yml', '.yaml')):
                return ruamel.yaml.load(value)

            self.fail("don't know how to read %s" % value)

        sp = value.split('=', 1)
        if len(sp) == 1:
            return {value: True}

        try:
            return {sp[0]: json.loads(sp[1])}
        except json.JSONDecodeError:
            return {sp[0]: sp[1]}


@main.command()
@click.option('--model', type=click.Path(file_okay=False, exists=True))
@click.option('--classes')
@click.option('--def', '-D', multiple=True, type=InputParamType())
@click.option('--refresh/--no-refresh', default=True)
@click.option('--force-backend-copy/--no-force-backend-copy', default=True)
@click.pass_context
@opts_obj
def up(ctx, opts):
    ctx.obj.rmtree_out_dir()

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
    ag_result = _debugdump.wrap(ag.produce, ctx.obj.debug_dir / 'terraform')(*opts["def"])
    tf_data = attrdict.AttrDict()
    tf_data += ag_result.get("terraform", {})

    data_external = {}

    path_str = os.pathsep.join(str(i.resolve()) for i in path)

    for k, v in ag_result.get("anygen", {}).items():
        query = {
            "path": path_str,
            "classes": v["classes"],
            "debug_dump": str(ctx.obj.debug_dir.joinpath('anygen.' + k).absolute())
        }
        for k2, v2 in v.get("args", {}).items():
            query["arg_" + k2] = '${jsonencode("%s")}' % v2

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

    with ctx.obj.terraform_dir.joinpath("main.tf").open(mode='w') as f:
        json.dump(main_tf_data, f, indent=4)

    body_module_dir = ctx.obj.terraform_dir / "body"
    body_module_dir.mkdir(exist_ok=True)

    with body_module_dir.joinpath("main.tf.json").open(mode='w') as f:
        json.dump(tf_data, f, indent=4)

    checked_tf(
        ctx.obj.terraform.init(
            capture_output=False,
            force_copy=opts.force_backend_copy
        ),
        'init'
    )
    checked_tf(
        ctx.obj.terraform.apply(
            capture_output=False,
            skip_plan=ctx.obj.yes,
            refresh=opts.refresh,
            no_color=python_terraform.IsNotFlagged
        ),
        'apply'
    )

    on_sucess_classes = ag_result.get("on_success", {}).get("classes", [])
    if on_sucess_classes:
        tf_result = ctx.obj.terraform.cmd(
            'state pull',
            capture_output=True,
        )
        checked_captured_tf(tf_result, 'state pull')
        tfstate = json.loads(tf_result[1])

        expected_outfiles = set()

        def jinjafilter_outfile(name):
            expected_outfiles.add(name)
            return ctx.obj.out_dir.joinpath(name).resolve()

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
            _debugdump.wrap(ag_success.produce, ctx.obj.debug_dir / 'on_success')(
                outputs=body_module_outputs
            )
        ctx.obj.out_dir.mkdir()
        for k, v in ag_success_result.get("files", {}).items():
            if isinstance(v, str):
                v = dict(content=v)

            expected_outfiles.discard(k)

            file_path = ctx.obj.out_dir / k
            file_path.write_text(v["content"])

            file_mode = v.get("chmod", None)
            if file_mode is not None:
                file_path.chmod(file_mode)

        if expected_outfiles:
            raise click.ClickException(
                "the following files have been referenced via 'outfile' but not produced: " +
                ', '.join(sorted(expected_outfiles))
            )

        text = ag_success_result.get("text", None)
        if text:
            if not ag_success_result.get("plaintext", False):
                text = ansimarkup.parse(text)
            click.echo(text)


@main.command()
@click.pass_context
@click.option('--refresh/--no-refresh', default=True)
@opts_obj
def down(ctx, opts):
    checked_tf(
        ctx.obj.terraform.destroy(
            capture_output=False,
            force=ctx.obj.yes,
            no_color=python_terraform.IsNotFlagged,
            refresh=opts.refresh
        ),
        'destroy'
    )

    ctx.obj.rmtree_out_dir()
