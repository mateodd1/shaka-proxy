"""Remux prepared MP4 tracks with libav inside a persistent CPU worker."""
from contextlib import ExitStack
from fractions import Fraction


def remux_mp4(video_path, audio_tracks, output_path, origin_t, timescale):
    import av

    offset = Fraction(1) - Fraction(origin_t, timescale)
    with ExitStack() as stack:
        tracks = [(video_path, 'video', '')] + [(path, 'audio', lang) for path, lang in audio_tracks]
        output = stack.enter_context(av.open(str(output_path), 'w', format='mpegts', options={
            'mpegts_copyts': '1', 'avoid_negative_ts': 'disabled', 'max_delay': '0',
        }))
        packets = []
        for index, (path, kind, language) in enumerate(tracks):
            source = stack.enter_context(av.open(str(path), options={'threads': '1'}))
            stream = next(s for s in source.streams if s.type == kind)
            target = output.add_stream_from_template(stream)
            if language:
                target.metadata['language'] = language
            if kind == 'audio':
                target.disposition = 1 if index == 1 else 0
            for packet in source.demux(stream):
                if packet.dts is None:
                    continue
                shift = round(offset / packet.time_base)
                packet.dts += shift
                if packet.pts is not None:
                    packet.pts += shift
                timestamp = packet.dts * packet.time_base
                packet.stream = target
                packets.append((timestamp, len(packets), packet))
        for _timestamp, _index, packet in sorted(packets):
            output.mux(packet)
