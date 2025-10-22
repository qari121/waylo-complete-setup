#!/usr/bin/env python3
from flask import Flask, request, jsonify
import subprocess
import json
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/api/audio/volume', methods=['GET', 'POST'])
def control_volume():
    if request.method == 'GET':
        # Get current volume levels
        try:
            # Get microphone volume
            mic_vol = subprocess.check_output(['amixer', 'get', 'Capture'], text=True)
            # Get speaker volume  
            speaker_vol = subprocess.check_output(['amixer', 'get', 'Master'], text=True)
            
            return jsonify({
                'microphone': parse_amixer_output(mic_vol),
                'speaker': parse_amixer_output(speaker_vol)
            })
        except Exception as e:
            log.error(f"Error getting volume: {e}")
            return jsonify({'error': str(e)}), 500
    
    elif request.method == 'POST':
        data = request.json
        try:
            if 'microphone' in data:
                vol = data['microphone']
                subprocess.run(['amixer', 'set', 'Capture', f'{vol}%'], check=True)
                log.info(f"Set microphone volume to {vol}%")
            
            if 'speaker' in data:
                vol = data['speaker']
                subprocess.run(['amixer', 'set', 'Master', f'{vol}%'], check=True)
                log.info(f"Set speaker volume to {vol}%")
            
            return jsonify({'success': True, 'message': 'Volume updated'})
        except Exception as e:
            log.error(f"Error setting volume: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/audio/mute', methods=['POST'])
def toggle_mute():
    data = request.json
    try:
        if data.get('microphone'):
            subprocess.run(['amixer', 'set', 'Capture', 'toggle'], check=True)
            log.info("Toggled microphone mute")
        if data.get('speaker'):
            subprocess.run(['amixer', 'set', 'Master', 'toggle'], check=True)
            log.info("Toggled speaker mute")
        
        return jsonify({'success': True, 'message': 'Mute toggled'})
    except Exception as e:
        log.error(f"Error toggling mute: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/audio/status', methods=['GET'])
def get_audio_status():
    try:
        # Get detailed audio status
        mic_status = subprocess.check_output(['amixer', 'get', 'Capture'], text=True)
        speaker_status = subprocess.check_output(['amixer', 'get', 'Master'], text=True)
        
        return jsonify({
            'microphone': {
                'volume': parse_amixer_output(mic_status),
                'muted': 'off' in mic_status.lower()
            },
            'speaker': {
                'volume': parse_amixer_output(speaker_status),
                'muted': 'off' in speaker_status.lower()
            }
        })
    except Exception as e:
        log.error(f"Error getting audio status: {e}")
        return jsonify({'error': str(e)}), 500

def parse_amixer_output(output):
    # Parse amixer output to extract volume percentage
    lines = output.split('\n')
    for line in lines:
        if '[' in line and ']' in line:
            start = line.find('[') + 1
            end = line.find(']')
            if start < end:
                vol_str = line[start:end]
                if '%' in vol_str:
                    return int(vol_str.replace('%', ''))
    return 0

if __name__ == '__main__':
    log.info("Starting Audio Controller API on port 5001")
    app.run(host='0.0.0.0', port=5001, debug=False)
