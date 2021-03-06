import io
import os
import struct

import sys

# Python 2 compat
try:
    num_types = (int, float, long)
    str = basestring
except NameError:
    num_types = (int, float)

from fitparse.processors import FitFileDataProcessor
from fitparse.profile import FIELD_TYPE_TIMESTAMP, MESSAGE_TYPES
from fitparse.records import (
    DataMessage, FieldData, FieldDefinition, DevFieldDefinition, DefinitionMessage, MessageHeader,
    BASE_TYPES, BASE_TYPE_BYTE, DevField,
    add_dev_data_id, add_dev_field_description, get_dev_type
)
from fitparse.utils import calc_crc, FitParseError, FitEOFError, FitCRCError, FitHeaderError

def get_field(message, is_dev, def_nums):
    if type(def_nums) is not list:
        def_nums = [def_nums]
    for field_data in message:
        fdef = field_data.field_def
        if (fdef is not None and
            hasattr(fdef, 'dev_data_index') == is_dev and
            fdef.def_num in def_nums):

            return field_data

def copy_field(src, dest):
    if src is not None and dest is not None:
        # src.value might have been postprocessed by unit/type/fild processors
        # so re-compute the value
        val = src.decode_raw_value()
        dest.set_value(val)
        # view postprocessed value in output
        dest.value = src.value

def copy_dev_to_native(message, dev_ids, n_id):
    dev_field = get_field(message, True, dev_ids)
    nat_field = get_field(message, False, n_id)
    copy_field(dev_field, nat_field)

def adjust_message(msg):
    #print msg.mesg_num
    #if msg.type == 'data' and msg.mesg_num == 20: # Record
    #    for field_data in msg:
    #        print field_data.field_def

    if msg.type == 'data' and msg.mesg_num == 20: # Record
        copy_dev_to_native(msg, [0, 23], 5) # distance

    if msg.type == 'data' and msg.mesg_num == 19:  # Lap
        copy_dev_to_native(msg, 4, 9) # total_distance

    if msg.type == 'data' and msg.mesg_num == 18:  # Session
        copy_dev_to_native(msg, [7, 25], 9) # total_distance
        copy_dev_to_native(msg, 21, 14)     # avg_speed


class FitFile(object):
    def __init__(self, fileish, check_crc=True, data_processor=None, out=None):
        self._verbose = False
        print("HELLO")

        if hasattr(fileish, 'read'):
            # BytesIO-like object
            self._file = fileish
            self._out = out
        elif isinstance(fileish, str):
            # Python2 - file path, file contents in the case of a TypeError
            # Python3 - file path
            try:
                self._file = open(fileish, 'rb')
            except TypeError:
                self._file = io.BytesIO(fileish)
        else:
            # Python 3 - file contents
            self._file = io.BytesIO(fileish)

        self.check_crc = check_crc
        self._processor = data_processor or FitFileDataProcessor()

        # Get total filesize
        self._file.seek(0, os.SEEK_END)
        self._filesize = self._file.tell()
        self._file.seek(0, os.SEEK_SET)

        # Start off by parsing the file header (sets initial attribute values)
        self._parse_file_header()

    def __del__(self):
        self.close()

    def close(self):
        if hasattr(self, "_file") and self._file and hasattr(self._file, "close"):
            self._file.close()
            self._file = None
        if  self._out and hasattr(self._out, "close"):
            self._out.close()
            self._out = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    ##########
    # Private low-level utility methods for reading of fit file

    def _read(self, size):
        if size <= 0:
            return None
        data = self._file.read(size)
        self._crc = calc_crc(data, self._crc)
        self._bytes_left -= len(data)
        return data

    def _write(self, data):
        if self._out and data:
            self._out.write(data)
            self._out_crc = calc_crc(data, self._out_crc)

    def _read_struct(self, fmt, endian='<', data=None, always_tuple=False):
        fmt_with_endian = "%s%s" % (endian, fmt)
        size = struct.calcsize(fmt_with_endian)
        if size <= 0:
            raise FitParseError("Invalid struct format: %s" % fmt_with_endian)

        if data is None:
            data = self._read(size)

        if size != len(data):
            raise FitEOFError("Tried to read %d bytes from .FIT file but got %d" % (size, len(data)))

        unpacked = struct.unpack(fmt_with_endian, data)
        # Flatten tuple if it's got only one value
        return unpacked if (len(unpacked) > 1) or always_tuple else unpacked[0]

    def _write_struct(self, data, fmt, endian='<'):
        if self._out is None:
            return

        fmt_with_endian = "%s%s" % (endian, fmt)
        size = struct.calcsize(fmt_with_endian)
        if size <= 0:
            raise FitParseError("Invalid struct format: %s" % fmt_with_endian)

        if type(data)==tuple:
            packed = struct.pack(fmt_with_endian, *data)
        else:
            packed = struct.pack(fmt_with_endian, data)
        self._write(packed)

    def _read_and_assert_crc(self, allow_zero=False):
        # CRC Calculation is little endian from SDK
        crc_expected, crc_actual = self._crc, self._read_struct('H')

        if (crc_actual != crc_expected) and not (allow_zero and (crc_actual == 0)):
            if self.check_crc:
                raise FitCRCError('CRC Mismatch [expected = 0x%04X, actual = 0x%04X]' % (
                    crc_expected, crc_actual))

    def _write_crc(self):
        self._write_struct((self._out_crc,), 'H')

    ##########
    # Private Data Parsing Methods

    def _parse_file_header(self):

        # Initialize data
        self._accumulators = {}
        self._bytes_left = -1
        self._complete = False
        self._compressed_ts_accumulator = 0
        self._crc = 0
        self._out_crc = 0
        self._local_mesgs = {}
        self._messages = []

        header_data = self._read(12)
        if header_data[8:12] != b'.FIT':
            raise FitHeaderError("Invalid .FIT File Header")

        # Larger fields are explicitly little endian from SDK
        header_size, protocol_ver_enc, profile_ver_enc, data_size = self._read_struct('2BHI4x', data=header_data)

        self._write(header_data)

        # Decode the same way the SDK does
        self.protocol_version = float("%d.%d" % (protocol_ver_enc >> 4, protocol_ver_enc & ((1 << 4) - 1)))
        self.profile_version = float("%d.%d" % (profile_ver_enc / 100, profile_ver_enc % 100))

        # Consume extra header information
        extra_header_size = header_size - 12
        if extra_header_size > 0:
            # Make sure extra field in header is at least 2 bytes to calculate CRC
            if extra_header_size < 2:
                raise FitHeaderError('Irregular File Header Size')

            # Consume extra two bytes of header and check CRC
            self._read_and_assert_crc(allow_zero=True)
            self._write_crc()

            # Consume any extra bytes, since header size "may be increased in
            # "future to add additional optional information" (from SDK)
            unknown = self._read(extra_header_size - 2)
            self._write(unknown)

        # After we've consumed the header, set the bytes left to be read
        self._bytes_left = data_size

    def _parse_message(self):
        # When done, calculate the CRC and return None
        if self._bytes_left <= 0:
            if not self._complete:
                self._read_and_assert_crc()
                self._write_crc()

            if self._file.tell() >= self._filesize:
                self._complete = True
                self.close()
                return None

            # Still have data left in the file - assuming chained fit files
            self._parse_file_header()
            return self._parse_message()

        header = self._parse_message_header()
        self._write_message_header(header)

        if header.is_definition:
            message = self._parse_definition_message(header)
            self._write_definition_message(message)
        else:
            message = self._parse_data_message(header)
            adjust_message(message)
            self._write_data_message(message)
            if message.mesg_type is not None:
                if message.mesg_type.name == 'developer_data_id':
                    add_dev_data_id(message)
                elif message.mesg_type.name == 'field_description':
                    add_dev_field_description(message)

        self._messages.append(message)
        return message

    def _parse_message_header(self):
        header = self._read_struct('B')

        if header & 0x80:  # bit 7: Is this record a compressed timestamp?
            return MessageHeader(
                is_definition=False,
                is_developer_data=False,
                local_mesg_num=(header >> 5) & 0x3,  # bits 5-6
                time_offset=header & 0x1F,  # bits 0-4
            )
        else:
            return MessageHeader(
                is_definition=bool(header & 0x40),  # bit 6
                is_developer_data=bool(header & 0x20), # bit 5
                local_mesg_num=header & 0xF,  # bits 0-3
                time_offset=None,
            )

    def _write_message_header(self, header):
        data = 0
        if header.time_offset is not None:
            data |= 0x80
            data |= header.local_mesg_num << 5
            data |= header.time_offset
        else:
            data |= 0x40 if header.is_definition else 0
            data |= 0x20 if header.is_developer_data else 0
            data |= header.local_mesg_num
        self._write_struct((data,), 'B')

    def _parse_definition_message(self, header):
        # Read reserved byte and architecture byte to resolve endian
        endian = '>' if self._read_struct('xB') else '<'
        # Read rest of header with endian awareness
        global_mesg_num, num_fields = self._read_struct('HB', endian=endian)
        mesg_type = MESSAGE_TYPES.get(global_mesg_num)
        field_defs = []

        for n in range(num_fields):
            field_def_num, field_size, base_type_num = self._read_struct('3B', endian=endian)
            # Try to get field from message type (None if unknown)
            field = mesg_type.fields.get(field_def_num) if mesg_type else None
            base_type = BASE_TYPES.get(base_type_num, BASE_TYPE_BYTE)

            if (field_size % base_type.size) != 0:
                # NOTE: we could fall back to byte encoding if there's any
                # examples in the wild. For now, just throw an exception
                raise FitParseError("Invalid field size %d for type '%s' (expected a multiple of %d)" % (
                    field_size, base_type.name, base_type.size))

            # If the field has components that are accumulators
            # start recording their accumulation at 0
            if field and field.components:
                for component in field.components:
                    if component.accumulate:
                        accumulators = self._accumulators.setdefault(global_mesg_num, {})
                        accumulators[component.def_num] = 0

            field_defs.append(FieldDefinition(
                field=field,
                def_num=field_def_num,
                base_type=base_type,
                size=field_size,
            ))

        dev_field_defs = []
        if header.is_developer_data:
            num_dev_fields = self._read_struct('B', endian=endian)
            for n in range(num_dev_fields):
                field_def_num, field_size, dev_data_index = self._read_struct('3B', endian=endian)
                field = get_dev_type(dev_data_index, field_def_num)
                dev_field_defs.append(DevFieldDefinition(
                    field=field,
                    dev_data_index=dev_data_index,
                    def_num=field_def_num,
                    size=field_size
                  ))

        def_mesg = DefinitionMessage(
            header=header,
            endian=endian,
            mesg_type=mesg_type,
            mesg_num=global_mesg_num,
            field_defs=field_defs,
            dev_field_defs=dev_field_defs,
        )
        self._local_mesgs[header.local_mesg_num] = def_mesg
        if self._verbose:
            print("DefinitionMessage", num_fields,len(dev_field_defs))
        return def_mesg

    def _write_definition_message(self, msg):
        self._write_struct((0, msg.mesg_num, len(msg.field_defs)), 'HHB')
        for fld in msg.field_defs:
            self._write_struct((fld.def_num, fld.size, fld.base_type.identifier), '3B')
        if msg.header.is_developer_data:
            self._write_struct(len(msg.dev_field_defs), 'B')
            for fld in msg.dev_field_defs:
                self._write_struct((fld.def_num, fld.size, fld.dev_data_index), '3B')


    def _parse_raw_values_from_data_message(self, def_mesg):
        # Go through mesg's field defs and read them
        raw_values = []
        for field_def in def_mesg.field_defs + def_mesg.dev_field_defs:
            base_type = field_def.base_type
            is_byte = base_type.name == 'byte'
            # Struct to read n base types (field def size / base type size)
            struct_fmt = '%d%s' % (
                field_def.size / base_type.size,
                base_type.fmt,
            )

            # Extract the raw value, ask for a tuple if it's a byte type
            raw_value = self._read_struct(
                struct_fmt, endian=def_mesg.endian, always_tuple=is_byte,
            )

            # If the field returns with a tuple of values it's definitely an
            # oddball, but we'll parse it on a per-value basis it.
            # If it's a byte type, treat the tuple as a single value
            if isinstance(raw_value, tuple) and not is_byte:
                raw_value = tuple(base_type.parse(rv) for rv in raw_value)
            else:
                # Otherwise, just scrub the singular value
                raw_value = base_type.parse(raw_value)

            raw_values.append(raw_value)
            if self._verbose:
                print("read ", field_def, struct_fmt, raw_value)
        return raw_values

    def _write_raw_values_from_data_message(self, def_mesg, raw_values):
        for field_def, raw_value in zip(def_mesg.field_defs + def_mesg.dev_field_defs, raw_values):
            base_type = field_def.base_type
            is_byte = base_type.name == 'byte'
            # Struct to read n base types (field def size / base type size)
            struct_fmt = '%d%s' % (
                field_def.size / base_type.size,
                base_type.fmt,
            )
            if is_byte and raw_value is None:
                raw_value = tuple(base_type.unparse(raw_value)*field_def.size)
            else:
                raw_value = base_type.unparse(raw_value)
            if self._verbose:
                print("write ", field_def, struct_fmt, raw_value)
            try:
                self._write_struct(raw_value, struct_fmt, endian=def_mesg.endian)
            except:
                print("Error in _write_struct:", raw_value, struct_fmt, def_mesg.endian)
                print(sys.exc_info()[0])
                raise

    @staticmethod
    def _resolve_subfield(field, def_mesg, raw_values):
        # Resolve into (field, parent) ie (subfield, field) or (field, None)
        if field.subfields:
            for sub_field in field.subfields:
                # Go through reference fields for this sub field
                for ref_field in sub_field.ref_fields:
                    # Go through field defs AND their raw values
                    for field_def, raw_value in zip(def_mesg.field_defs, raw_values):
                        # If there's a definition number AND raw value match on the
                        # reference field, then we return this subfield
                        if (field_def.def_num == ref_field.def_num) and (ref_field.raw_value == raw_value):
                            return sub_field, field
        return field, None

    def _apply_scale_offset(self, field, raw_value):
        # Apply numeric transformations (scale+offset)
        if isinstance(raw_value, tuple):
            # Contains multiple values, apply transformations to all of them
            return tuple(self._apply_scale_offset(field, x) for x in raw_value)
        elif isinstance(raw_value, num_types):
            if field.scale:
                raw_value = float(raw_value) / field.scale
            if field.offset:
                raw_value = raw_value - field.offset
        return raw_value

    @staticmethod
    def _apply_compressed_accumulation(raw_value, accumulation, num_bits):
        max_value = (1 << num_bits)
        max_mask = max_value - 1
        base_value = raw_value + (accumulation & ~max_mask)

        if raw_value < (accumulation & max_mask):
            base_value += max_value

        return base_value

    def _parse_data_message(self, header):
        def_mesg = self._local_mesgs.get(header.local_mesg_num)
        if not def_mesg:
            raise FitParseError('Got data message with invalid local message type %d' % (
                header.local_mesg_num))

        raw_values = self._parse_raw_values_from_data_message(def_mesg)
        field_datas = []  # TODO: I don't love this name, update on DataMessage too

        # TODO: Maybe refactor this and make it simpler (or at least broken
        #       up into sub-functions)
        for field_def, raw_value in zip(def_mesg.field_defs + def_mesg.dev_field_defs, raw_values):
            field, parent_field = field_def.field, None
            if field:
                field, parent_field = self._resolve_subfield(field, def_mesg, raw_values)

                # Resolve component fields
                if field.components:
                    for component in field.components:
                        # Render its raw value
                        cmp_raw_value = component.render(raw_value)

                        # Apply accumulated value
                        if component.accumulate and cmp_raw_value is not None:
                            accumulator = self._accumulators[def_mesg.mesg_num]
                            cmp_raw_value = self._apply_compressed_accumulation(
                                cmp_raw_value, accumulator[component.def_num], component.bits,
                            )
                            accumulator[component.def_num] = cmp_raw_value

                        # Apply scale and offset from component, not from the dynamic field
                        # as they may differ
                        cmp_raw_value = self._apply_scale_offset(component, cmp_raw_value)

                        # Extract the component's dynamic field from def_mesg
                        cmp_field = def_mesg.mesg_type.fields[component.def_num]

                        # Resolve a possible subfield
                        cmp_field, cmp_parent_field = self._resolve_subfield(cmp_field, def_mesg, raw_values)
                        cmp_value = cmp_field.render(cmp_raw_value)

                        # Plop it on field_datas
                        field_datas.append(
                            FieldData(
                                field_def=None,
                                field=cmp_field,
                                parent_field=cmp_parent_field,
                                value=cmp_value,
                                raw_value=cmp_raw_value,
                            )
                        )

                # TODO: Do we care about a base_type and a resolved field mismatch?
                # My hunch is we don't
                value = self._apply_scale_offset(field, field.render(raw_value))
            else:
                value = raw_value

            # Update compressed timestamp field
            if (field_def.def_num == FIELD_TYPE_TIMESTAMP.def_num) and (raw_value is not None):
                self._compressed_ts_accumulator = raw_value

            field_datas.append(
                FieldData(
                    field_def=field_def,
                    field=field,
                    parent_field=parent_field,
                    value=value,
                    raw_value=raw_value,
                )
            )

        # Apply timestamp field if we got a header
        if header.time_offset is not None:
            ts_value = self._compressed_ts_accumulator = self._apply_compressed_accumulation(
                header.time_offset, self._compressed_ts_accumulator, 5,
            )
            field_datas.append(
                FieldData(
                    field_def=None,
                    field=FIELD_TYPE_TIMESTAMP,
                    parent_field=None,
                    value=FIELD_TYPE_TIMESTAMP.render(ts_value),
                    raw_value=ts_value,
                )
            )

        # Apply data processors
        for field_data in field_datas:
            # Apply type name processor
            self._processor.run_type_processor(field_data)
            self._processor.run_field_processor(field_data)
            self._processor.run_unit_processor(field_data)

        data_message = DataMessage(header=header, def_mesg=def_mesg, fields=field_datas)
        self._processor.run_message_processor(data_message)

        if self._verbose:
            print("DataMessage", len(field_datas))
        return data_message

    def _write_data_message(self, msg):
        raw_values = []
        for fld in msg.fields:
            if fld.field_def is not None:
                raw_values.append(fld.raw_value)

        self._write_raw_values_from_data_message(msg.def_mesg, raw_values)



    ##########
    # Public API

    def get_messages(self, name=None, with_definitions=False, as_dict=False, verbose=False):
        self._verbose=verbose
        if with_definitions:  # with_definitions implies as_dict=False
            as_dict = False

        if name is not None:
            if isinstance(name, (tuple, list)):
                names = name
            else:
                names = [name]

            # Convert any string numbers in names to ints
            # TODO: Revisit Python2/3 str/bytes typecheck issues
            names = set([
                int(n) if (isinstance(n, str) and n.isdigit()) else n
                for n in names
            ])

        def should_yield(message):
            if with_definitions or message.type == 'data':
                # name arg is None we return all
                if name is None:
                    return True
                else:
                    if (message.name in names) or (message.mesg_num in names):
                        return True
            return False

        # Yield all parsed messages first
        for message in self._messages:
            if should_yield(message):
                yield message.as_dict() if as_dict else message

        # If there are unparsed messages, yield those too
        while not self._complete:
            message = self._parse_message()
            if message and should_yield(message):
                yield message.as_dict() if as_dict else message

    @property
    def messages(self):
        # TODO: could this be more efficient?
        return list(self.get_messages())

    def parse(self):
        while self._parse_message():
            pass

    def __iter__(self):
        return self.get_messages()


# TODO: Create subclasses like Activity and do per-value monkey patching
# for example local_timestamp to adjust timestamp on a per-file basis
