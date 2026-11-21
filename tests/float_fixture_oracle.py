import io, struct
from thrift.Thrift import TType
from thrift.protocol.TCompactProtocol import TCompactProtocol
from thrift.transport.TTransport import TMemoryBuffer

def read_value(p, t):
    if t == TType.STRUCT:
        result=[]; p.readStructBegin()
        while True:
            _, kind, field=p.readFieldBegin()
            if kind == TType.STOP: break
            result.append((field, kind, read_value(p,kind)))
            p.readFieldEnd()
        p.readStructEnd(); return result
    if t in (TType.LIST, TType.SET):
        kind,n=p.readListBegin() if t==TType.LIST else p.readSetBegin()
        result=(kind,[read_value(p,kind) for _ in range(n)])
        p.readListEnd() if t==TType.LIST else p.readSetEnd()
        return result
    if t == TType.MAP:
        k,v,n=p.readMapBegin(); result=(k,v,[(read_value(p,k),read_value(p,v)) for _ in range(n)]);p.readMapEnd();return result
    return {TType.BOOL:p.readBool,TType.BYTE:p.readByte,TType.I16:p.readI16,TType.I32:p.readI32,TType.I64:p.readI64,TType.DOUBLE:p.readDouble,TType.STRING:p.readBinary}[t]()

def write_value(p,t,v):
    if t==TType.STRUCT:
        p.writeStructBegin('')
        for field,kind,value in v:
            p.writeFieldBegin('',kind,field);write_value(p,kind,value);p.writeFieldEnd()
        p.writeFieldStop();p.writeStructEnd();return
    if t in (TType.LIST,TType.SET):
        kind,values=v
        p.writeListBegin(kind,len(values)) if t==TType.LIST else p.writeSetBegin(kind,len(values))
        for value in values:write_value(p,kind,value)
        p.writeListEnd() if t==TType.LIST else p.writeSetEnd();return
    if t==TType.MAP:
        k,kind,values=v;p.writeMapBegin(k,kind,len(values))
        for key,value in values:write_value(p,k,key);write_value(p,kind,value)
        p.writeMapEnd();return
    {TType.BOOL:p.writeBool,TType.BYTE:p.writeByte,TType.I16:p.writeI16,TType.I32:p.writeI32,TType.I64:p.writeI64,TType.DOUBLE:p.writeDouble,TType.STRING:p.writeBinary}[t](v)

def load(data):
    size=struct.unpack('<I',data[-8:-4])[0];return read_value(TCompactProtocol(TMemoryBuffer(data[-8-size:-8])),TType.STRUCT)
def dump(data, fields):
    size=struct.unpack('<I',data[-8:-4])[0];buf=TMemoryBuffer();write_value(TCompactProtocol(buf),TType.STRUCT,fields);footer=buf.getvalue();return data[:-8-size]+footer+struct.pack('<I',len(footer))+b'PAR1'
