# Python SCALE Codec Library
#
# Copyright 2018-2020 Stichting Polkascan (Polkascan Foundation).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from abc import ABC, abstractmethod
from typing import Optional
from scalecodec.exceptions import RemainingScaleBytesNotEmptyException, InvalidScaleTypeValueException


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):

        if 'config_id' in kwargs:
            instance_key = kwargs['config_id']
        else:
            instance_key = cls

        if instance_key not in cls._instances:
            cls._instances[instance_key] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[instance_key]


class RuntimeConfigurationObject:

    @classmethod
    def all_subclasses(cls, class_):
        return set(class_.__subclasses__()).union(
            [s for c in class_.__subclasses__() for s in cls.all_subclasses(c)])

    def __init__(self, config_id=None, ss58_format=None, only_primitives_on_init=False):
        self.config_id = config_id
        self.type_registry = {'types': {}}
        self.__initial_state = False
        self.clear_type_registry()
        self.active_spec_version_id = None
        self.chain_id = None

        self.only_primitives_on_init = only_primitives_on_init
        self.ss58_format = ss58_format

    @classmethod
    def convert_type_string(cls, name):

        name = re.sub(r'T::', "", name)
        name = re.sub(r'^T::', "", name, flags=re.IGNORECASE)
        name = re.sub(r'<T>', "", name, flags=re.IGNORECASE)
        name = re.sub(r'<T as Trait>::', "", name, flags=re.IGNORECASE)
        name = re.sub(r'<T as Trait<I>>::', "", name, flags=re.IGNORECASE)
        name = re.sub(r'<T as Config>::', "", name, flags=re.IGNORECASE)
        name = re.sub(r'<T as Config<I>>::', "", name, flags=re.IGNORECASE)
        name = re.sub(r'\n', "", name)
        name = re.sub(r'^(grandpa|session|slashing|limits|beefy_primitives|xcm::opaque)::', "", name)
        name = re.sub(r'VecDeque<', "Vec<", name, flags=re.IGNORECASE)
        name = re.sub(r'^Box<(.+)>$', r'\1', name, flags=re.IGNORECASE)

        if name == '()':
            return "Null"
        if name.lower() in ['vec<u8>', '&[u8]', "& 'static[u8]"]:
            return "Bytes"
        if name.lower() == '<lookup as staticlookup>::source':
            return 'LookupSource'
        if name.lower() == '<balance as hascompact>::type':
            return 'Compact<Balance>'
        if name.lower() == '<blocknumber as hascompact>::type':
            return 'Compact<BlockNumber>'
        if name.lower() == '<moment as hascompact>::type':
            return 'Compact<Moment>'
        if name.lower() == '<inherentofflinereport as inherentofflinereport>::inherent':
            return 'InherentOfflineReport'

        return name

    def get_decoder_class(self, type_string: str, spec_version_id='default'):

        if type_string.strip() == '':
            return None

        type_string = self.convert_type_string(type_string)

        decoder_class = self.type_registry.get('types', {}).get(type_string.lower(), None)

        if not decoder_class:

            # Type string containg subtype
            if type_string[-1:] == '>':

                # Extract sub types
                type_parts = re.match(r'^([^<]*)<(.+)>$', type_string)

                if type_parts:
                    type_parts = type_parts.groups()

                if type_parts:
                    # Create dynamic class for Part1<Part2> based on Part1 and set class variable Part2 as sub_type
                    base_class = self.type_registry.get('types', {}).get(type_parts[0].lower(), None)
                    if base_class:
                        decoder_class = type(type_string, (base_class,), {'sub_type': type_parts[1]})

            # Custom tuples
            elif type_string != '()' and type_string[0] == '(' and type_string[-1] == ')':

                decoder_class = type(type_string, (self.get_decoder_class('tuple'),), {
                    'type_string': type_string
                })

                decoder_class.build_type_mapping()

            elif type_string[0] == '[' and type_string[-1] == ']':
                type_parts = re.match(r'^\[([A-Za-z0-9]+); ([0-9]+)\]$', type_string)

                if type_parts:
                    type_parts = type_parts.groups()

                if type_parts:
                    # Create dynamic class for e.g. [u8; 4] resulting in array of u8 with 4 elements
                    decoder_class = type(type_string, (self.get_decoder_class('FixedLengthArray'),), {
                        'sub_type': type_parts[0],
                        'element_count': int(type_parts[1])
                    })

        if decoder_class:
            # Attach RuntimeConfigurationObject to new class
            decoder_class.runtime_config = self

        return decoder_class

    def clear_type_registry(self):

        if not self.__initial_state:
            self.type_registry = {'types': {}}

            # Class names that contains '<' are excluded because of a side effect that is introduced in
            # get_decoder_class: "Create dynamic class for Part1<Part2> based on Part1 and set class variable Part2 as
            # sub_type" which won't get reset because class definitions always remain globally

            self.type_registry['types'].update(
                {cls.__name__.lower(): cls for cls in self.all_subclasses(ScaleDecoder) if '<' not in cls.__name__}
            )

        self.__initial_state = True

    def update_type_registry_types(self, types_dict):
        from scalecodec.types import Enum, Struct, Set, Tuple

        self.__initial_state = False

        for type_string, decoder_class_data in types_dict.items():

            if type(decoder_class_data) == dict:

                # Create dynamic decoder class
                base_cls = None

                if decoder_class_data.get('base_class'):
                    base_cls = self.get_decoder_class(decoder_class_data['base_class'])
                    if base_cls is None:
                        raise ValueError(f"Specified base_class '{decoder_class_data['base_class']}' for type " +
                                         f"'{type_string}' not found")

                if decoder_class_data['type'] == 'struct':

                    if base_cls is None:
                        base_cls = Struct

                    decoder_class = type(type_string, (base_cls,), {
                        'type_mapping': decoder_class_data.get('type_mapping')
                    })

                elif decoder_class_data['type'] == 'tuple':

                    if base_cls is None:
                        base_cls = Tuple

                    decoder_class = type(type_string, (base_cls,), {
                        'type_mapping': decoder_class_data.get('type_mapping')
                    })

                elif decoder_class_data['type'] == 'enum':

                    if base_cls is None:
                        base_cls = Enum

                    decoder_class = type(type_string, (base_cls,), {
                        'value_list': decoder_class_data.get('value_list'),
                        'type_mapping': decoder_class_data.get('type_mapping')
                    })

                elif decoder_class_data['type'] == 'set':

                    if base_cls is None:
                        base_cls = Set

                    decoder_class = type(type_string, (base_cls,), {
                        'value_list': decoder_class_data.get('value_list'),
                        'value_type': decoder_class_data.get('value_type', 'u64')
                    })

                else:
                    raise NotImplementedError("Dynamic decoding type '{}' not supported".format(
                        decoder_class_data['type'])
                    )
            else:
                decoder_class = self.get_decoder_class(decoder_class_data)

            self.type_registry['types'][type_string.lower()] = decoder_class

    def update_type_registry(self, type_registry):

        # Set runtime ID if set
        self.active_spec_version_id = type_registry.get('runtime_id')

        # Set chain ID if set
        self.chain_id = type_registry.get('chain_id')

        self.type_registry['versioning'] = type_registry.get('versioning')
        self.type_registry['runtime_upgrades'] = type_registry.get('runtime_upgrades')

        # Update types
        if 'types' in type_registry:
            self.update_type_registry_types(type_registry.get('types'))

    def set_active_spec_version_id(self, spec_version_id):

        if spec_version_id != self.active_spec_version_id:

            self.active_spec_version_id = spec_version_id

            # Updated type registry with versioned types
            for versioning_item in self.type_registry.get('versioning') or []:
                # Check if versioning item is in current version range
                if versioning_item['runtime_range'][0] <= spec_version_id and \
                        (not versioning_item['runtime_range'][1] or versioning_item['runtime_range'][1] >= spec_version_id):
                    # Update types in type registry
                    self.update_type_registry_types(versioning_item['types'])

    def get_runtime_id_from_upgrades(self, block_number: int) -> Optional[int]:
        """
        Retrieve runtime_id for given block_number if runtime_upgrades are specified in the type registry

        Parameters
        ----------
        block_number

        Returns
        -------
        Runtime id
        """
        if self.type_registry.get('runtime_upgrades'):

            if block_number > self.type_registry['runtime_upgrades'][-1][0]:
                return

            for max_block_number, runtime_id in reversed(self.type_registry['runtime_upgrades']):
                if block_number >= max_block_number and runtime_id != -1:
                    return runtime_id

    def set_runtime_upgrades_head(self, block_number: int):
        """
        Sets head for given block_number to last runtime_id in runtime_upgrades cache

        Parameters
        ----------
        block_number

        Returns
        -------

        """
        if self.type_registry.get('runtime_upgrades'):
            if self.type_registry['runtime_upgrades'][-1][1] == -1:
                self.type_registry['runtime_upgrades'][-1][0] = block_number
            elif block_number > self.type_registry['runtime_upgrades'][-1][0]:
                self.type_registry['runtime_upgrades'].append([block_number, -1])

    def get_decoder_class_for_scale_info_definition(self, type_string, scale_info_type):

        decoder_class = None
        base_decoder_class = None

        # Check if base decoder class is defined for path
        if 'path' in scale_info_type:
            path_string = '::'.join(scale_info_type["path"])
            base_decoder_class = self.get_decoder_class(path_string)

            if base_decoder_class:
                decoder_class = type(type_string, (base_decoder_class,), {})
                decoder_class.process_scale_info_definition(scale_info_type)

                return decoder_class

        if "primitive" in scale_info_type["def"]:
            decoder_class = self.get_decoder_class(scale_info_type["def"]["primitive"])

        elif 'array' in scale_info_type['def']:

            if base_decoder_class is None:
                base_decoder_class = self.get_decoder_class('FixedLengthArray')

            decoder_class = type(type_string, (base_decoder_class,), {
                'sub_type': f"scale_info::{scale_info_type['def']['array']['type']}",
                'element_count': scale_info_type['def']['array']['len']
            })

        elif 'composite' in scale_info_type['def']:

            type_mapping = []

            base_type_string = 'Tuple'

            if 'fields' in scale_info_type['def']['composite']:

                fields = scale_info_type['def']['composite']['fields']

                if all(['name' in f for f in fields]):
                    base_type_string = 'Struct'
                    type_mapping = [[field['name'], f"scale_info::{field['type']}"] for field in fields]

                else:
                    base_type_string = 'Tuple'
                    type_mapping = [f"scale_info::{field['type']}" for field in fields]

            if base_decoder_class is None:
                base_decoder_class = self.get_decoder_class(base_type_string)

            decoder_class = type(type_string, (base_decoder_class,), {
                'type_mapping': type_mapping
            })

        elif 'sequence' in scale_info_type['def']:
            # Vec
            decoder_class = type(type_string, (self.get_decoder_class('Vec'),), {
                'sub_type': f"scale_info::{scale_info_type['def']['sequence']['type']}"
            })

        elif 'variant' in scale_info_type['def']:
            # Enum
            type_mapping = []

            if 'variants' in scale_info_type['def']['variant']:

                for variant in scale_info_type['def']['variant']['variants']:

                    if 'fields' in variant:
                        if len(variant['fields']) == 1:
                            enum_value = f"scale_info::{variant['fields'][0]['type']}"
                        else:
                            field_str = ', '.join([f"scale_info::{f['type']}" for f in variant['fields']])
                            enum_value = f"({field_str})"
                    else:
                        enum_value = 'Null'

                    type_mapping.append(
                        [variant['name'], enum_value]
                    )

            decoder_class = type(type_string, (self.get_decoder_class("Enum"),), {
                'type_mapping': type_mapping
            })

        elif 'tuple' in scale_info_type['def']:

            type_mapping = [f"scale_info::{f}" for f in scale_info_type['def']['tuple']]

            decoder_class = type(type_string, (self.get_decoder_class('Tuple'),), {
                'type_mapping': type_mapping
            })

        elif 'compact' in scale_info_type['def']:
            # Compact
            decoder_class = type(type_string, (self.get_decoder_class('Compact'),), {
                'sub_type': f"scale_info::{scale_info_type['def']['compact']['type']}"
            })

        elif 'phantom' in scale_info_type['def']:
            decoder_class = type(type_string, (self.get_decoder_class('Null'),), {})

        return decoder_class

    def update_from_scale_info_types(self, scale_info_types: list, prefix: str = ''):
        for idx, scale_info_type in enumerate(scale_info_types):

            type_string = f"scale_info::{idx}"

            decoder_class = self.get_decoder_class_for_scale_info_definition(
                type_string, scale_info_type
            )

            if decoder_class is None:
                raise NotImplementedError(f"No decoding class found for scale type {idx}")

            if decoder_class:
                self.type_registry['types'][f"scale_info::{idx}"] = decoder_class

    def add_runtime_metadata_dict_to_type_registry(self, metadata_dict):
        return self.update_from_scale_info_types(metadata_dict[1]["V14"]['types']['types'], prefix='runtime')

    def add_contract_metadata_dict_to_type_registry(self, metadata_dict):
        prefix = f"ink::{metadata_dict['source']['hash']}"
        return self.update_from_scale_info_types(metadata_dict['types'], prefix=prefix)


class ScaleBytes:

    def __init__(self, data):
        self.offset = 0

        if type(data) is bytearray:
            self.data = data
        elif type(data) is bytes:
            self.data = bytearray(data)
        elif type(data) is str and data[0:2] == '0x':
            self.data = bytearray.fromhex(data[2:])
        else:
            raise ValueError("Provided data is not in supported format: provided '{}'".format(type(data)))

        self.length = len(self.data)

    def get_next_bytes(self, length):
        data = self.data[self.offset:self.offset + length]
        self.offset += length
        return data

    def get_remaining_bytes(self):
        data = self.data[self.offset:]
        self.offset = self.length
        return data

    def get_remaining_length(self):
        return self.length - self.offset

    def reset(self):
        self.offset = 0

    def __str__(self):
        return "0x{}".format(self.data.hex())

    def __eq__(self, other):
        if not hasattr(other, 'data'):
            return False
        return self.data == other.data

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return "<{}(data=0x{})>".format(self.__class__.__name__, self.data.hex())

    def __add__(self, data):

        if type(data) == ScaleBytes:
            return ScaleBytes(self.data + data.data)

        if type(data) == bytes:
            data = bytearray(data)
        elif type(data) == str and data[0:2] == '0x':
            data = bytearray.fromhex(data[2:])

        if type(data) == bytearray:
            return ScaleBytes(self.data + data)

    def to_hex(self):
        return f'0x{self.data.hex()}'


class ScaleDecoder(ABC):

    type_string = None

    type_mapping = None

    debug = False

    sub_type = None

    runtime_config = None

    def __init__(self, data, sub_type=None, runtime_config=None):

        if sub_type:
            self.sub_type = sub_type

        if self.type_mapping is None and self.type_string:
            self.build_type_mapping()

        if data:
            assert(type(data) == ScaleBytes)

        if runtime_config:
            self.runtime_config = runtime_config

        if not self.runtime_config:
            # if no runtime config is provided, fallback on singleton
            self.runtime_config = RuntimeConfiguration()

        self.data = data
        self.raw_value = ''
        self.value = None
        self.value_object = None
        self.data_start_offset = None
        self.data_end_offset = None

    @staticmethod
    def is_primitive(type_string: str) -> bool:
        return type_string in ('bool', 'u8', 'u16', 'u32', 'u64', 'u128', 'u256', 'i8', 'i16', 'i32', 'i64', 'i128',
                               'i256', 'h160', 'h256', 'h512', '[u8; 4]', '[u8; 4]', '[u8; 8]', '[u8; 16]', '[u8; 32]',
                               '&[u8]')

    @classmethod
    def build_type_mapping(cls):

        if cls.type_string and cls.type_string[0] == '(' and cls.type_string[-1] == ')':
            type_mapping = ()

            tuple_contents = cls.type_string[1:-1]

            # replace subtype types
            sub_types = re.search(r'([A-Za-z]+[<][^>]*[>])', tuple_contents)
            if sub_types:
                sub_types = sub_types.groups()
                for sub_type in sub_types:
                    tuple_contents = tuple_contents.replace(sub_type, sub_type.replace(',', '|'))

            for tuple_element in tuple_contents.split(','):
                type_mapping += (tuple_element.strip().replace('|', ','),)

            cls.type_mapping = type_mapping

    def get_next_bytes(self, length):
        data = self.data.get_next_bytes(length)
        self.raw_value += data.hex()
        return data

    def get_next_u8(self):
        return int.from_bytes(self.get_next_bytes(1), byteorder='little')

    def get_next_bool(self):
        data = self.get_next_bytes(1)
        if data not in [b'\x00', b'\x01']:
            raise InvalidScaleTypeValueException('Invalid value for datatype "bool"')
        return data == b'\x01'

    def get_remaining_bytes(self):
        data = self.data.get_remaining_bytes()
        self.raw_value += data.hex()
        return data

    def get_used_bytes(self):
        return self.data.data[self.data_start_offset:self.data_end_offset]

    @abstractmethod
    def process(self):
        raise NotImplementedError

    def decode(self, check_remaining=True):
        self.data_start_offset = self.data.offset
        self.value = self.process()

        if self.value_object is None:
            self.value_object = self.value

        self.data_end_offset = self.data.offset

        if check_remaining and self.data.offset != self.data.length:
            raise RemainingScaleBytesNotEmptyException(
                f'Decoding <{self.__class__.__name__}> - Current offset: {self.data.offset} / length: {self.data.length}'
            )

        if self.data.offset > self.data.length:
            raise RemainingScaleBytesNotEmptyException(
                f'Decoding <{self.__class__.__name__}> - No more bytes available (needed: {self.data.offset} / total: {self.data.length})'
            )

        return self.value

    def __str__(self):
        return str(self.serialize()) or ''

    def __repr__(self):
        return "<{}(value={})>".format(self.__class__.__name__, self.serialize())

    def encode(self, value=None):

        if value is not None:
            self.value = value

        self.data = self.process_encode(self.value)
        return self.data

    def process_encode(self, value):
        raise NotImplementedError("Encoding not implemented for this ScaleType")

    @classmethod
    def get_decoder_class(cls, type_string, data=None, runtime_config=None, **kwargs):
        """

        Parameters
        ----------
        type_string
        data
        runtime_config
        kwargs

        Returns
        -------
        ScaleType
        """

        if not runtime_config:
            runtime_config = RuntimeConfiguration()

        decoder_class = runtime_config.get_decoder_class(
            type_string,
            spec_version_id=kwargs.get('spec_version_id', 'default')
        )
        if decoder_class:
            return decoder_class(data=data, runtime_config=runtime_config, **kwargs)

        raise NotImplementedError('Decoder class for "{}" not found'.format(type_string))

    # TODO rename to decode_type (confusing when encoding is introduced)
    def process_type(self, type_string, **kwargs):
        obj = self.get_decoder_class(type_string, self.data, runtime_config=self.runtime_config, **kwargs)
        obj.decode(check_remaining=False)
        return obj

    def serialize(self):
        return self.value

    @classmethod
    def convert_type(cls, name):
        return RuntimeConfigurationObject.convert_type_string(name)


class RuntimeConfiguration(RuntimeConfigurationObject, metaclass=Singleton):
    pass


class ScaleType(ScaleDecoder, ABC):

    def __init__(self, data=None, sub_type=None, metadata=None, runtime_config=None):
        """

        Parameters
        ----------
        data: ScaleBytes
        sub_type: str
        metadata: MetadataDecoder
        runtime_config: RuntimeConfigurationObject
        """
        self.metadata = metadata
        if not data:
            data = ScaleBytes(bytearray())
        super().__init__(data, sub_type, runtime_config=runtime_config)

    @classmethod
    def process_scale_info_definition(cls, scale_info_definition: dict):
        pass


