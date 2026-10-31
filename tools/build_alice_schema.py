"""Build a dependency-closed Alice API descriptor set from a protodump bundle."""
import argparse
from pathlib import Path

from google.protobuf import descriptor_pb2


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('input',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    source=descriptor_pb2.FileDescriptorSet.FromString(args.input.read_bytes())
    byname={file.name:file for file in source.file}
    selected=set()
    def include(name):
        if name in selected:return
        selected.add(name)
        for dependency in byname[name].dependency:include(dependency)
    for name in ('alice/protos/api/alicekit/protocol/client/client_message.proto','alice/protos/api/alicekit/protocol/server/server_message.proto'):include(name)
    result=descriptor_pb2.FileDescriptorSet()
    for file in source.file:
        if file.name in selected:result.file.add().CopyFrom(file)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(result.SerializeToString())
    print(f'{len(result.file)} files, {len(result.SerializeToString())} bytes')


if __name__=='__main__':main()
