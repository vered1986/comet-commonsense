import os
import setuptools

from setuptools.command.install import install

with open("README.md", "r") as fh:
    long_description = fh.read()


class MyInstall(install):
    def run(self):
        install.run(self)
        script = os.path.join(os.getcwd(), "setup/download.sh")
        os.system(f"bash {script}")


setuptools.setup(
    name="comet-commonsense", # Replace with your own username
    version="2.0",
    author="This version by Vered Shwartz. Original version by Antoine Bosselut.",
    description="COMET: Commonsense Transformers for Automatic Knowledge Graph Construction",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/vered1986/comet-commonsense",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
    cmdclass={'install': MyInstall}
)
