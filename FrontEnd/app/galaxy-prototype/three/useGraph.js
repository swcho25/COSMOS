import { useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useGalaxy } from '../store/useGalaxy';
import { buildGraph } from '../data/universe';
import { buildLiveUniverse } from '../data/live-universe';

export function useLiveUniverse() {
  const liveFeed = useGalaxy((s) => s.liveFeed);
  const activeNewsId = useGalaxy((s) => s.activeNewsId);
  return useMemo(
    () => (liveFeed?.items?.length ? buildLiveUniverse(liveFeed.items, activeNewsId) : null),
    [liveFeed, activeNewsId]
  );
}

/**
 * 현재 시점 + 필터로 관계 그래프를 계산한다.
 * 3D 뷰와 우측 패널이 같은 결과를 봐야 하므로 훅으로 공유한다.
 * (실 API 연동 시 여기만 useQuery 로 바꾸면 된다)
 */
export function useGraph() {
  const { day, threshold, activeTypes, onlySerendipity, activeSector } = useGalaxy(
    useShallow((s) => ({
      day: s.day,
      threshold: s.threshold,
      activeTypes: s.activeTypes,
      onlySerendipity: s.onlySerendipity,
      activeSector: s.activeSector,
    }))
  );
  const typeKey = [...activeTypes].sort().join(',');
  const liveUniverse = useLiveUniverse();

  return useMemo(() => {
    if (liveUniverse) {
      let edges = liveUniverse.edges.filter(
        (edge) => Math.abs(edge.corr) >= threshold && activeTypes.has(edge.type)
      );
      if (onlySerendipity) edges = edges.filter((edge) => edge.basis === 'modeled');
      if (activeSector) {
        edges = edges.filter(
          (edge) =>
            liveUniverse.companyById[edge.a]?.sector === activeSector &&
            liveUniverse.companyById[edge.b]?.sector === activeSector
        );
      }
      return edges;
    }
    let edges = buildGraph(day, { threshold, types: activeTypes });
    if (onlySerendipity) edges = edges.filter((e) => e.serendipity);
    return edges;
    // activeTypes 는 Set 이라 참조가 매번 바뀐다 → 직렬화한 typeKey 로 비교
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day, threshold, typeKey, onlySerendipity, activeSector, liveUniverse]);
}
