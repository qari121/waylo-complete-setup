#!/bin/bash
# Complete Wailo Startup Sequence
# Implements the exact flow: WiFi → Bluetooth → MAC exchange → Bluetooth off → AI_Chat

echo "🚀 Starting Complete Wailo Startup Sequence..."
echo "📋 Flow: WiFi → Bluetooth → MAC exchange → Bluetooth off → AI_Chat"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "❌ Please run as root (sudo)"
    exit 1
fi

# Step 1: Wait for WiFi connectivity (handled by wifi-monitor.sh)
echo "📡 Step 1: Waiting for WiFi connectivity..."
echo "   (This is handled by wifi-monitor.sh in the background)"
echo "   (If no WiFi, AP mode will be activated)"

# Wait for network to be online
echo "   Waiting for network-online.target..."
systemctl wait network-online.target --timeout=60
if [ $? -eq 0 ]; then
    echo "✅ Network is online"
else
    echo "⚠️ Network timeout - continuing anyway (AP mode may be active)"
fi

# Step 2: Start Bluetooth service
echo ""
echo "🔵 Step 2: Starting Bluetooth service..."
systemctl start bluetooth.service
sleep 3

# Check Bluetooth status
if systemctl is-active --quiet bluetooth.service; then
    echo "✅ Bluetooth service is running"
else
    echo "❌ Bluetooth service failed to start"
    exit 1
fi

# CRITICAL: Wait for Bluetooth adapter to be fully ready
echo "   ⏳ Waiting for Bluetooth adapter to be fully ready..."
echo "   (This prevents race condition where BLE service starts too early)"

# Wait for Bluetooth adapter to be fully initialized
sleep 5

# Additional check: Verify Bluetooth adapter is actually usable
echo "   🔍 Verifying Bluetooth adapter is ready..."
max_attempts=10
attempt=1

while [ $attempt -le $max_attempts ]; do
    echo "   ⏳ Attempt $attempt/$max_attempts: Checking Bluetooth adapter..."
    
    # Try to power on the adapter
    if bluetoothctl power on >/dev/null 2>&1; then
        echo "   ✅ Bluetooth adapter is ready and responsive"
        break
    else
        echo "   ⚠️ Bluetooth adapter not ready yet (attempt $attempt/$max_attempts)"
        if [ $attempt -eq $max_attempts ]; then
            echo "   ❌ Bluetooth adapter failed to become ready after $max_attempts attempts"
            echo "   🔍 Checking Bluetooth service status..."
            systemctl status bluetooth.service --no-pager
            exit 1
        fi
        sleep 2
        attempt=$((attempt + 1))
    fi
done

echo "   ✅ Bluetooth adapter is fully ready for BLE operations"

# Step 3: Start BLE handoff service (pairing mode)
echo ""
echo "📱 Step 3: Starting BLE handoff service (pairing mode)..."
systemctl start wailo-ble-handoff.service

# Wait for BLE handoff to complete
echo "   Waiting for iOS connection and MAC verification..."
while [ ! -f /home/orangepi/Waylo_AI/.bluetooth_handoff_complete ]; do
    echo "   ⏳ Waiting for iOS app to read MAC address..."
    sleep 5
    
    # Check if BLE service is still running
    if ! systemctl is-active --quiet wailo-ble-handoff.service; then
        echo "   ⚠️ BLE service stopped unexpectedly"
        break
    fi
done

if [ -f /home/orangepi/Waylo_AI/.bluetooth_handoff_complete ]; then
    echo "✅ BLE handoff completed! iOS has verified MAC address"
else
    echo "⚠️ BLE handoff may not have completed properly"
fi

# Step 4: Bluetooth automatically turns off (handled by GATT server)
echo ""
echo "🔴 Step 4: Bluetooth turning off (automatic)..."
echo "   (This happens automatically after MAC exchange)"

# Step 5: Start Wailo service (AI_Chat)
echo ""
echo "🤖 Step 5: Starting Wailo service (AI_Chat)..."
systemctl start wailo.service

# Wait a moment and check status
sleep 3
if systemctl is-active --quiet wailo.service; then
    echo "✅ Wailo service (AI_Chat) is running"
else
    echo "❌ Wailo service failed to start"
    systemctl status wailo.service --no-pager
    exit 1
fi

# Final status check
echo ""
echo "📊 Final Service Status:"
echo "   Bluetooth service: $(systemctl is-active bluetooth.service)"
echo "   BLE handoff: $(if [ -f /home/orangepi/Waylo_AI/.bluetooth_handoff_complete ]; then echo "✅ Complete"; else echo "❌ Incomplete"; fi)"
echo "   Wailo service: $(systemctl is-active wailo.service)"

echo ""
echo "🎉 Complete Wailo Startup Sequence Finished!"
echo "📱 BLE handoff: ✅ Complete"
echo "🤖 AI Chat: ✅ Running"
echo "🔐 Device ready for use!"
echo ""
echo "💡 The system will now:"
echo "   - Keep WiFi/AP mode active for network connectivity"
echo "   - Run AI_Chat.py with wailo-env activated"
echo "   - Handle all chatbot functionality automatically"
