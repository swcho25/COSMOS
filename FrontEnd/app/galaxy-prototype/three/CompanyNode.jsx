import { useEffect, useRef, useMemo, useState } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import LabelSprite from './LabelSprite';
import { useDotTexture } from './textures';
import { SECTORS } from '../data/universe';
import { useGalaxy } from '../store/useGalaxy';

function companyMonogram(value = '') {
  const label = String(value).replace(/\([^)]*\)/g, '').trim();
  const latinWords = label.match(/[A-Za-z0-9]+/g);
  if (latinWords?.length > 1) {
    return latinWords.slice(0, 2).map((word) => word[0]).join('').toUpperCase();
  }
  if (latinWords?.[0]) return latinWords[0].slice(0, 2).toUpperCase();
  return label.replace(/\s+/g, '').slice(0, 2) || '?';
}

function createFallbackLogoTexture(label, accent) {
  if (typeof document === 'undefined') return null;

  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 256;
  const context = canvas.getContext('2d');
  const accentColor = new THREE.Color(accent || '#5fb8ff');
  const deepColor = accentColor.clone().multiplyScalar(0.24).lerp(new THREE.Color('#07111f'), 0.52);
  const midColor = accentColor.clone().multiplyScalar(0.62);
  const gradient = context.createRadialGradient(92, 72, 12, 128, 128, 184);

  gradient.addColorStop(0, `#${midColor.getHexString()}`);
  gradient.addColorStop(1, `#${deepColor.getHexString()}`);
  context.fillStyle = gradient;
  context.fillRect(0, 0, canvas.width, canvas.height);

  context.beginPath();
  context.arc(128, 128, 108, 0, Math.PI * 2);
  context.strokeStyle = 'rgba(255, 255, 255, 0.18)';
  context.lineWidth = 6;
  context.stroke();

  context.fillStyle = '#ffffff';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.font = '800 88px Pretendard, "Malgun Gothic", Arial, sans-serif';
  context.shadowColor = 'rgba(0, 0, 0, 0.5)';
  context.shadowBlur = 12;
  context.fillText(companyMonogram(label), 128, 132);

  const fallbackTexture = new THREE.CanvasTexture(canvas);
  fallbackTexture.colorSpace = THREE.SRGBColorSpace;
  fallbackTexture.minFilter = THREE.LinearFilter;
  fallbackTexture.magFilter = THREE.LinearFilter;
  fallbackTexture.anisotropy = 8;
  return fallbackTexture;
}

export function CompanyLogoBadge({ url, radius, active, dim = 1, fallbackLabel, fallbackColor }) {
  const badge = useRef();
  const cameraInLocalSpace = useMemo(() => new THREE.Vector3(), []);
  const [texture, setTexture] = useState(null);
  const fallbackTexture = useMemo(
    () => createFallbackLogoTexture(fallbackLabel, fallbackColor),
    [fallbackColor, fallbackLabel]
  );

  useEffect(() => () => fallbackTexture?.dispose(), [fallbackTexture]);

  useEffect(() => {
    setTexture(null);
    if (!url) {
      return undefined;
    }

    let cancelled = false;
    let loadedTexture = null;
    const loader = new THREE.TextureLoader();
    loader.setCrossOrigin('anonymous');
    loader.load(
      url,
      (nextTexture) => {
        loadedTexture = nextTexture;
        nextTexture.colorSpace = THREE.SRGBColorSpace;
        nextTexture.minFilter = THREE.LinearFilter;
        nextTexture.magFilter = THREE.LinearFilter;
        nextTexture.anisotropy = 8;
        if (cancelled) nextTexture.dispose();
        else setTexture(nextTexture);
      },
      undefined,
      () => {
        if (!cancelled) setTexture(null);
      }
    );

    return () => {
      cancelled = true;
      loadedTexture?.dispose();
    };
  }, [url]);

  useFrame(({ camera }) => {
    if (!badge.current) return;
    const parent = badge.current.parent;
    if (!parent) return;
    parent.worldToLocal(cameraInLocalSpace.copy(camera.position));
    badge.current.position.copy(cameraInLocalSpace.normalize().multiplyScalar(radius * 1.025));
    badge.current.quaternion.copy(camera.quaternion);
  });

  const displayedTexture = texture ?? fallbackTexture;
  if (!displayedTexture) return null;

  const logoRadius = radius * (active ? 0.79 : 0.76);
  return (
    <group ref={badge} renderOrder={20}>
      <mesh position={[0, 0, -0.018]} renderOrder={20}>
        <circleGeometry args={[logoRadius * 1.18, 64]} />
        <meshBasicMaterial
          color="#f7f9fc"
          transparent
          opacity={0.96 * dim}
          side={THREE.DoubleSide}
          depthTest={false}
          depthWrite={false}
          toneMapped={false}
        />
      </mesh>
      <mesh renderOrder={21}>
        <circleGeometry args={[logoRadius, 64]} />
        <meshBasicMaterial
          map={displayedTexture}
          transparent
          opacity={dim}
          side={THREE.DoubleSide}
          depthTest={false}
          depthWrite={false}
          toneMapped={false}
        />
      </mesh>
      <mesh position={[0, 0, 0.018]} renderOrder={22}>
        <ringGeometry args={[logoRadius * 1.08, logoRadius * 1.17, 64]} />
        <meshBasicMaterial
          color={active ? '#ffffff' : '#b9dcff'}
          transparent
          opacity={(active ? 0.95 : 0.7) * dim}
          side={THREE.DoubleSide}
          depthTest={false}
          depthWrite={false}
          toneMapped={false}
        />
      </mesh>
    </group>
  );
}

/**
 * 기업 = 항성.
 *  - 코어 구체 + 가산합성 헤일로 + 회전 링
 *  - 현재 시점에 뉴스가 있으면 충격파 링이 퍼져나간다 (호재=앰버 / 악재=시안)
 */
export default function CompanyNode({
  company,
  position,
  radius,
  label,
  dim = 1,
  active = false,
  focused = false,
  brightness = 1,
  news = null, // { tone, ageDays }
  logoUrl = null,
  onHover,
  onClick,
  showLabel = true,
}) {
  const grp = useRef();
  const halo = useRef();
  const ring = useRef();
  const shock = useRef();
  const coreMaterial = useRef();
  const shellMaterial = useRef();
  const appliedPlanetBrightness = useRef(Number.NaN);
  const appliedAmbientColor = useRef(-1);
  const dot = useDotTexture();
  const sectorColor = SECTORS[company.sector].color;
  const ambientColor = useMemo(
    () => new THREE.Color(sectorColor).multiplyScalar(brightness),
    [brightness, sectorColor]
  );
  const seed = useMemo(() => {
    const numeric = parseInt(company.id, 10);
    if (Number.isFinite(numeric)) return (numeric % 997) / 997;
    let value = 0;
    for (let index = 0; index < company.id.length; index += 1) {
      value = (value * 31 + company.id.charCodeAt(index)) | 0;
    }
    return Math.abs(value % 997) / 997;
  }, [company.id]);

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime;
    const planetBrightness = useGalaxy.getState().planetBrightness;
    const ambientHex = ambientColor.getHex();
    if (
      planetBrightness !== appliedPlanetBrightness.current
      || ambientHex !== appliedAmbientColor.current
    ) {
      coreMaterial.current?.color.copy(ambientColor).multiplyScalar(planetBrightness);
      shellMaterial.current?.color.copy(ambientColor).multiplyScalar(planetBrightness);
      appliedPlanetBrightness.current = planetBrightness;
      appliedAmbientColor.current = ambientHex;
    }
    if (grp.current) {
      const target = active ? 1.22 : 1;
      grp.current.scale.lerp({ x: target, y: target, z: target }, Math.min(1, dt * 8));
      grp.current.position.y = position[1] + Math.sin(t * 0.5 + seed * 9) * 0.35;
    }
    if (halo.current) {
      const pulse = 1 + Math.sin(t * 1.4 + seed * 12) * 0.06;
      const s = radius * (active ? 7.4 : 5.4) * pulse;
      halo.current.scale.set(s, s, 1);
      halo.current.material.opacity = (active ? 0.9 : 0.5) * dim * planetBrightness;
    }
    if (ring.current) {
      ring.current.rotation.z += dt * 0.35;
      ring.current.rotation.x = Math.PI / 2.6 + Math.sin(t * 0.25 + seed * 5) * 0.22;
      ring.current.material.opacity = (active ? 0.85 : 0.32) * dim * planetBrightness;
    }
    if (shock.current) {
      if (news) {
        // 뉴스 발생 후 경과일에 따라 퍼지는 충격파
        const k = THREE.MathUtils.clamp(news.ageDays / 12, 0, 1);
        const s = radius * (1.4 + k * 7.5);
        shock.current.scale.set(s, s, s);
        shock.current.material.opacity = (1 - k) * 0.75 * dim * planetBrightness;
        shock.current.visible = true;
      } else {
        shock.current.visible = false;
      }
    }
  });

  return (
    <group ref={grp} position={position}>
      {/* 헤일로 */}
      <sprite ref={halo}>
        <spriteMaterial
          map={dot}
          color={ambientColor}
          transparent
          opacity={0.6}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          toneMapped={false}
        />
      </sprite>

      {/* 코어 */}
      <mesh>
        <icosahedronGeometry args={[radius, 3]} />
        <meshBasicMaterial ref={coreMaterial} color={ambientColor} toneMapped={false} />
      </mesh>
      <mesh scale={1.35}>
        <icosahedronGeometry args={[radius, 2]} />
        <meshBasicMaterial
          ref={shellMaterial}
          color={ambientColor}
          transparent
          opacity={0.22 * dim}
          wireframe
          toneMapped={false}
        />
      </mesh>

      <CompanyLogoBadge
        url={logoUrl}
        radius={radius}
        active={active || focused}
        dim={1}
        fallbackLabel={company.name}
        fallbackColor={sectorColor}
      />

      {/* 궤도 링 */}
      <mesh ref={ring}>
        <torusGeometry args={[radius * 2.1, radius * 0.045, 8, 72]} />
        <meshBasicMaterial
          color={ambientColor}
          transparent
          opacity={0.32}
          depthWrite={false}
          toneMapped={false}
        />
      </mesh>

      {/* 뉴스 충격파 */}
      <mesh ref={shock} visible={false}>
        <sphereGeometry args={[1, 24, 16]} />
        <meshBasicMaterial
          color={news?.tone > 0 ? '#ffb04a' : '#54e0ff'}
          transparent
          opacity={0}
          wireframe
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          toneMapped={false}
        />
      </mesh>

      {showLabel && (
        <LabelSprite
          text={label ?? company.name}
          color={active ? '#ffffff' : '#cfe0f5'}
          size={active ? 2.1 : 1.7}
          opacity={(active ? 1 : 0.8) * dim}
          position={[0, radius * 2.9 + 1.4, 0]}
        />
      )}

      {/* 클릭 판정용 (보이지 않는 큰 구) */}
      <mesh
        onPointerOver={(e) => {
          e.stopPropagation();
          onHover?.(company.id);
        }}
        onPointerOut={(e) => {
          e.stopPropagation();
          onHover?.(null);
        }}
        onClick={(e) => {
          e.stopPropagation();
          onClick?.(company.id);
        }}
      >
        <sphereGeometry args={[Math.max(radius * 2.4, 2.6), 12, 8]} />
        <meshBasicMaterial visible={false} />
      </mesh>

      {focused && (
        <mesh rotation={[Math.PI / 2, 0, 0]}>
          <ringGeometry args={[radius * 3.1, radius * 3.28, 64]} />
          <meshBasicMaterial
            color="#ffffff"
            transparent
            opacity={0.5}
            side={THREE.DoubleSide}
            toneMapped={false}
          />
        </mesh>
      )}
    </group>
  );
}
