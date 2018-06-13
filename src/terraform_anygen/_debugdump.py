import ruamel.yaml


def wrap(f, path):
    if path is None:
        return f

    def dump(what, name):
        path \
            .with_suffix(path.suffix + '.%s.yaml' % name) \
            .write_text(ruamel.yaml.dump(what, default_flow_style=0))

    def wrapped(*a, **kw):
        dump(dict(a=a, kw=kw), 'in')
        result = f(*a, **kw)
        dump(result, 'out')
        return result

    return wrapped
