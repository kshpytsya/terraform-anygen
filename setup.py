from setuptools import setup, find_packages

setup(
    name="terraform-anygen",
    setup_requires=["setuptools_scm"],
    use_scm_version=True,
    python_requires=">=3.6, <=3.7",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    entry_points={"console_scripts": ["terraform-anygen = terraform_anygen._cli:main"]},
    install_requires=[
        'click~=6.7',
        'python-terraform~=0.10',
        'tinyshar>=0.10',
        'anygen',
        'attrdict>=2,<3',
        'ansimarkup~=1.4'
    ]
)
